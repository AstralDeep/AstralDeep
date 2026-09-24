"""Per-user, per-agent encrypted credential store backed by Postgres, using Fernet for
orchestrator-readable OAuth secrets and ECIES for end-to-end agent-only secrets; used
throughout remote_compute and remote_control agents.
"""

import os
import time
import logging
from typing import Dict, List, Optional

from cryptography.fernet import Fernet

from shared.crypto import (
    encrypt_for_agent, ec_public_key_from_jwk, is_e2e_encrypted,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

logger = logging.getLogger("CredentialManager")


class CredentialNotConfigured(Exception):
    pass


class CredentialUndecryptable(Exception):
    pass


class CredentialManager:
    def __init__(
        self,
        db=None,
        data_dir: str = None,
        database_url: str = None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ):
        if database_url is not None:
            raise ValueError(
                "CredentialManager no longer constructs database runtimes; "
                "inject the application Plane runtime"
            )
        if db is None and plane_runtime is None:
            raise ValueError("CredentialManager requires the application Plane runtime")

        self.data_dir = data_dir
        self._fernet = self._init_encryption()

        repository, runtime = repository_from(
            "credentials",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._credentials = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )

        self._agent_public_keys: Dict[str, dict] = {}

    def _init_encryption(self) -> Fernet:
        env_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
        if env_key:
            return Fernet(env_key.encode())

        key_dir = self.data_dir or os.path.join(os.path.dirname(__file__), '..', 'data')
        key_path = os.path.join(key_dir, ".credential_key")

        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            with open(key_path, "wb") as f:
                f.write(key)
            logger.info("Generated new credential encryption key")

        return Fernet(key)

    def register_agent_public_key(self, agent_id: str, jwk: dict):
        self._agent_public_keys[agent_id] = jwk
        logger.info(f"Registered ECIES public key for agent '{agent_id}'")

    def has_agent_public_key(self, agent_id: str) -> bool:
        return agent_id in self._agent_public_keys

    def set_credential(self, user_id: str, agent_id: str, key: str, value: str, e2e: bool = True):
        if e2e and agent_id in self._agent_public_keys:
            agent_pub = ec_public_key_from_jwk(self._agent_public_keys[agent_id])
            encrypted = encrypt_for_agent(value, agent_pub)
        else:
            encrypted = self._fernet.encrypt(value.encode()).decode()

        now = int(time.time() * 1000)
        self._credentials.call(
            self._credentials.repository.upsert_credential,
            owner_id=user_id,
            agent_id=agent_id,
            credential_key=key,
            encrypted_value=encrypted,
            updated_at=now,
        )
        mode = "E2E/ECIES" if encrypted.startswith("e2e:") else "Fernet"
        logger.info(f"Credential set ({mode}): user={user_id} agent={agent_id} key={key}")

    def get_credential(self, user_id: str, agent_id: str, key: str) -> Optional[str]:
        row = self._credentials.call(
            self._credentials.repository.get_credential,
            owner_id=user_id,
            agent_id=agent_id,
            credential_key=key,
        )
        if row is None:
            return None
        value = row.encrypted_value
        if is_e2e_encrypted(value):
            logger.error(f"Cannot decrypt E2E credential on orchestrator: key={key}")
            return None
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except Exception as e:
            logger.error(f"Failed to decrypt credential: user={user_id} agent={agent_id} key={key}: {e}")
            return None

    def get_agent_credentials(self, user_id: str, agent_id: str) -> Dict[str, str]:
        rows = self._credentials.call(
            self._credentials.repository.list_credentials,
            owner_id=user_id,
            agent_id=agent_id,
            limit=1000,
        )
        result = {}
        for row in rows:
            key = row.credential_key
            if key.startswith('_'):
                continue
            value = row.encrypted_value
            if is_e2e_encrypted(value):
                continue
            try:
                result[key] = self._fernet.decrypt(value.encode()).decode()
            except Exception as e:
                logger.error(f"Failed to decrypt credential {key}: {e}")
        return result

    def get_agent_credentials_encrypted(self, user_id: str, agent_id: str) -> Dict[str, str]:
        rows = self._credentials.call(
            self._credentials.repository.list_credentials,
            owner_id=user_id,
            agent_id=agent_id,
            limit=1000,
        )
        result = {}
        for row in rows:
            key = row.credential_key
            if key.startswith('_'):
                continue
            result[key] = row.encrypted_value
        return result

    def delete_credential(self, user_id: str, agent_id: str, key: str):
        self._credentials.call(
            self._credentials.repository.delete_credential,
            owner_id=user_id,
            agent_id=agent_id,
            credential_key=key,
        )
        logger.info(f"Credential deleted: user={user_id} agent={agent_id} key={key}")

    def list_credential_keys(self, user_id: str, agent_id: str) -> List[str]:
        rows = self._credentials.call(
            self._credentials.repository.list_credential_keys,
            owner_id=user_id,
            agent_id=agent_id,
            limit=1000,
        )
        return list(rows)

    def set_bulk_credentials(self, user_id: str, agent_id: str, credentials: Dict[str, str], e2e: bool = True):
        for key, value in credentials.items():
            self.set_credential(user_id, agent_id, key, value, e2e=e2e)

    def remove_agent_credentials(self, user_id: str, agent_id: str):
        self._credentials.call(
            self._credentials.repository.delete_agent_credentials,
            owner_id=user_id,
            agent_id=agent_id,
        )
        logger.info(f"All credentials removed: user={user_id} agent={agent_id}")

    def set_machine_credential(self, machine_id: str, owner_user_id: str, cred_type: str,
                               secret: str, passphrase: Optional[str] = None):
        enc_secret = self._fernet.encrypt(secret.encode()).decode()
        enc_pass = self._fernet.encrypt(passphrase.encode()).decode() if passphrase else None
        now = int(time.time() * 1000)
        repository = self._credentials.repository
        with self._credentials.transaction() as transaction:
            current = repository.get_machine_credential(
                transaction,
                owner_id=owner_user_id,
                machine_id=machine_id,
            )
            if current is None:
                repository.create_machine_credential(
                    transaction,
                    owner_id=owner_user_id,
                    machine_id=machine_id,
                    credential_type=cred_type,
                    encrypted_secret=enc_secret,
                    encrypted_passphrase=enc_pass,
                    created_at=now,
                )
            else:
                repository.compare_and_set_machine_credential(
                    transaction,
                    owner_id=owner_user_id,
                    machine_id=machine_id,
                    expected_updated_at=current.updated_at,
                    credential_type=cred_type,
                    encrypted_secret=enc_secret,
                    encrypted_passphrase=enc_pass,
                    updated_at=max(now, current.updated_at + 1),
                )
        logger.info(f"Machine credential set: machine={machine_id} type={cred_type}")

    def get_machine_credential(
        self,
        machine_id: str,
        owner_user_id: str,
    ) -> Optional[Dict[str, Optional[str]]]:
        row = self._credentials.call(
            self._credentials.repository.get_machine_credential,
            owner_id=owner_user_id,
            machine_id=machine_id,
        )
        if row is None:
            return None
        try:
            secret = self._fernet.decrypt(row.encrypted_secret.encode()).decode()
            passphrase = None
            if row.encrypted_passphrase:
                passphrase = self._fernet.decrypt(
                    row.encrypted_passphrase.encode()
                ).decode()
        except Exception as e:
            raise CredentialUndecryptable(str(machine_id)) from e
        return {
            "cred_type": row.credential_type,
            "secret": secret,
            "passphrase": passphrase,
        }

    def delete_machine_credential(self, machine_id: str, owner_user_id: str):
        self._credentials.call(
            self._credentials.repository.delete_machine_credential,
            owner_id=owner_user_id,
            machine_id=machine_id,
        )
        logger.info(f"Machine credential deleted: machine={machine_id}")

    def remove_machine_credentials_for_user(self, owner_user_id: str) -> int:
        removed = self._credentials.call(
            self._credentials.repository.delete_owner_machine_credentials,
            owner_id=owner_user_id,
        )
        logger.info(f"Machine credentials removed for user={owner_user_id}: {removed}")
        return removed

    def migrate_to_e2e(self, agent_id: str) -> int:
        if agent_id not in self._agent_public_keys:
            logger.error(f"Cannot migrate: no public key for agent '{agent_id}'")
            return 0

        agent_pub = ec_public_key_from_jwk(self._agent_public_keys[agent_id])
        migrated = 0
        after_credential_id = 0
        while True:
            with self._credentials.transaction() as transaction:
                rows = self._credentials.repository.list_agent_credentials_for_reencryption(
                    transaction,
                    agent_id=agent_id,
                    after_credential_id=after_credential_id,
                    limit=200,
                )
                for row in rows:
                    after_credential_id = row.credential_id
                    key = row.credential_key
                    value = row.encrypted_value

                    if key.startswith('_') or is_e2e_encrypted(value):
                        continue

                    try:
                        plaintext = self._fernet.decrypt(value.encode()).decode()
                        encrypted = encrypt_for_agent(plaintext, agent_pub)
                        now = int(time.time() * 1000)
                        expected = row.updated_at
                        updated = now if expected is None else max(now, expected + 1)
                        self._credentials.repository.compare_and_set_ciphertext(
                            transaction,
                            owner_id=row.owner_id,
                            agent_id=agent_id,
                            credential_key=key,
                            expected_updated_at=expected,
                            encrypted_value=encrypted,
                            updated_at=updated,
                        )
                        migrated += 1
                    except Exception as e:
                        logger.error(f"Migration failed for {key}: {e}")
            if len(rows) < 200:
                break

        logger.info(f"Migrated {migrated} credentials to E2E for agent '{agent_id}'")
        return migrated
