"""File transfer via rsync (with scp fallback).

Handles pull and push of WIP directories between local and remote.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import attrs

if TYPE_CHECKING:
    from voxhub_client.ssh import SshTarget


@attrs.define
class RsyncTransfer:
    """Rsync-based file transfer.

    Parameters
    ----------
    target : SshTarget
        Parsed SSH target for remote host info.
    """

    target: SshTarget

    def _ssh_option(self) -> str:
        """Build the SSH command string for rsync's ``-e`` flag."""
        parts = ['ssh']
        if self.target.port is not None:
            parts.extend(['-p', str(self.target.port)])
        return ' '.join(parts)

    def pull(
        self,
        remote_path: str,
        local_path: str,
        *,
        progress: bool = True,
    ) -> None:
        """Pull files from the remote server.

        Parameters
        ----------
        remote_path : str
            Path on the remote server (directory).
        local_path : str
            Local destination directory.
        progress : bool
            Show rsync progress.
        """
        # Ensure trailing slashes for directory sync.
        if not remote_path.endswith('/'):
            remote_path += '/'
        if not local_path.endswith('/'):
            local_path += '/'

        src = f'{self.target.ssh_destination}:{remote_path}'

        cmd = ['rsync', '-az']
        if progress:
            cmd.append('--progress')
        cmd.extend(['-e', self._ssh_option(), src, local_path])

        subprocess.run(cmd, check=True)

    def push(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: bool = True,
    ) -> None:
        """Push files to the remote server.

        Parameters
        ----------
        local_path : str
            Local source directory.
        remote_path : str
            Path on the remote server (directory).
        progress : bool
            Show rsync progress.
        """
        if not local_path.endswith('/'):
            local_path += '/'
        if not remote_path.endswith('/'):
            remote_path += '/'

        dst = f'{self.target.ssh_destination}:{remote_path}'

        cmd = ['rsync', '-az']
        if progress:
            cmd.append('--progress')
        cmd.extend(['-e', self._ssh_option(), local_path, dst])

        subprocess.run(cmd, check=True)


@attrs.define
class ScpFallback:
    """SCP-based fallback transfer.

    Parameters
    ----------
    target : SshTarget
        Parsed SSH target.
    """

    target: SshTarget

    def pull(self, remote_path: str, local_path: str) -> None:
        """Pull a file or directory from the remote server."""
        src = f'{self.target.ssh_destination}:{remote_path}'
        cmd = ['scp', '-r']
        if self.target.port is not None:
            cmd.extend(['-P', str(self.target.port)])
        cmd.extend([src, local_path])
        subprocess.run(cmd, check=True)

    def push(self, local_path: str, remote_path: str) -> None:
        """Push a file or directory to the remote server."""
        dst = f'{self.target.ssh_destination}:{remote_path}'
        cmd = ['scp', '-r']
        if self.target.port is not None:
            cmd.extend(['-P', str(self.target.port)])
        cmd.extend([local_path, dst])
        subprocess.run(cmd, check=True)
