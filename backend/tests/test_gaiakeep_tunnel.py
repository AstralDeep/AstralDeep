"""Verifies pinned SSH tunnel admission, fixed loopback destinations and cleanup without network contact."""

from unittest.mock import MagicMock

import paramiko
import pytest
from orchestrator import remote_transport


@pytest.fixture
def target():
    return remote_transport.MachineTarget('m', 'login', '198.51.100.7', 40000, 'caller', 'password',
                                          secret='private', host_key_fingerprint='SHA256:existing-pin')


def test_pinned_loopback_channel_and_cleanup(target, monkeypatch):
    channel, ssh = MagicMock(), MagicMock()
    ssh.get_transport.return_value.open_channel.return_value = channel
    monkeypatch.setattr(remote_transport.ParamikoTransport, '_connect', lambda *a: (ssh, None))
    with remote_transport.ParamikoTransport().open_tunnel(target, 8282) as actual:
        assert actual is channel
    ssh.get_transport.return_value.open_channel.assert_called_once_with(
        'direct-tcpip', ('127.0.0.1', 8282), ('127.0.0.1', 0), timeout=10)
    channel.close.assert_called_once()
    ssh.close.assert_called_once()


def test_tunnel_creation_failure_closes_ssh(target, monkeypatch):
    ssh = MagicMock()
    ssh.get_transport.return_value.open_channel.side_effect = OSError('unavailable')
    monkeypatch.setattr(remote_transport.ParamikoTransport, '_connect', lambda *a: (ssh, None))
    with pytest.raises(OSError), remote_transport.ParamikoTransport().open_tunnel(target, 8282):
        pass
    ssh.close.assert_called_once()


def test_channel_close_failure_still_closes_ssh(target, monkeypatch):
    channel, ssh = MagicMock(), MagicMock()
    channel.close.side_effect = OSError('cleanup')
    ssh.get_transport.return_value.open_channel.return_value = channel
    monkeypatch.setattr(remote_transport.ParamikoTransport, '_connect', lambda *a: (ssh, None))
    with pytest.raises(OSError), remote_transport.ParamikoTransport().open_tunnel(target, 8282):
        pass
    ssh.close.assert_called_once()


@pytest.mark.parametrize('port', [False, 0, 65536, '8282'])
def test_bad_port_is_not_dialed(target, port, monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(remote_transport.ParamikoTransport, '_connect', connect)
    with pytest.raises(ValueError), remote_transport.ParamikoTransport().open_tunnel(target, port):
        pass
    connect.assert_not_called()


def test_missing_host_pin_is_not_dialed(target, monkeypatch):
    target.host_key_fingerprint = None
    connect = MagicMock()
    monkeypatch.setattr(remote_transport.ParamikoTransport, '_connect', connect)
    with pytest.raises(remote_transport.HostKeyMismatch), remote_transport.ParamikoTransport().open_tunnel(target, 8282):
        pass
    connect.assert_not_called()


def test_failed_authentication_closes_client(target, monkeypatch):
    ssh = MagicMock()
    ssh.connect.side_effect = paramiko.AuthenticationException('private-detail')
    monkeypatch.setattr(paramiko, 'SSHClient', lambda: ssh)
    monkeypatch.setattr(remote_transport.net_guard, 'assert_ssh_target_allowed', lambda *a: ['198.51.100.7'])
    with pytest.raises(paramiko.AuthenticationException):
        remote_transport.ParamikoTransport()._connect(target, 10)
    ssh.close.assert_called_once()
