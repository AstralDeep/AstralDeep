"""Checks the primitives against published test vectors that do not come from this reference."""

from __future__ import annotations

import pytest

from astral_mesh_ref import crypto, keys
from astral_mesh_ref.errors import MeshError


def test_ed25519_rfc8032_test_one() -> None:
    seed = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    )
    assert (
        crypto.ed25519_public(seed).hex()
        == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    assert crypto.ed25519_sign(seed, b"").hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )


def test_hkdf_rfc5869_case_one() -> None:
    okm = crypto.hkdf(
        bytes([0x0B] * 22), bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), bytes(range(13)), 42
    )
    assert (
        okm.hex()
        == "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_x25519_rfc7748() -> None:
    alice = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    )
    bob_public = bytes.fromhex(
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    )
    assert (
        crypto.x25519_public(alice).hex()
        == "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )
    assert (
        crypto.x25519_shared(alice, bob_public).hex()
        == "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    )


def test_p256_rfc6979_signature_up_to_low_s() -> None:
    scalar = int("C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721", 16)
    signature = crypto.p256_sign(scalar, b"sample")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    assert r == int(
        "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716", 16
    )
    assert s in (
        int("F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8", 16),
        crypto.P256_ORDER
        - int("F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8", 16),
    )
    assert s <= crypto.HALF_ORDER
    assert crypto.p256_verify(crypto.p256_public(scalar), b"sample", signature)


@pytest.mark.parametrize(
    ("fill", "words"),
    [
        (
            0x7F,
            "legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth title",
        ),
        (
            0x80,
            "letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic bless",
        ),
        (0x00, "abandon " * 23 + "art"),
        (0xFF, "zoo " * 23 + "vote"),
    ],
)
def test_bip39_published_vectors(fill: int, words: str) -> None:
    secret = bytes([fill]) * 32
    assert keys.encode_words(secret) == words
    assert keys.decode_words(words) == secret


def test_wordlist_is_the_english_bip39_list() -> None:
    import hashlib
    from importlib import resources

    data = resources.files("astral_mesh_ref").joinpath("wordlist_en.txt").read_bytes()
    assert (
        hashlib.sha256(data).hexdigest()
        == "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
    )


def test_aead_rejects_bad_lengths() -> None:
    with pytest.raises(MeshError):
        crypto.aead_seal(b"short", bytes(24), b"x", b"")
    with pytest.raises(MeshError):
        crypto.aead_open(bytes(32), bytes(24), b"tiny", b"")
