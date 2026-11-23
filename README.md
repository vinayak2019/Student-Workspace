# HPC Student Workspace — Bridges-2 + ACES

A Windows-inspired browser file manager, molecular editor/viewer, and SLURM job manager for PSC Bridges-2 and Texas A&M ACES.

## Deploy to Streamlit Community Cloud

Commit `app.py`, `aces_connection.py`, `molecule_viewer.py`, `molecule_builder.py`, and `requirements.txt` to the same GitHub directory. Launch using `app.py` as the entrypoint.

### Bridges-2 settings

Keep your previously configured Streamlit Secrets (`B2_WORKSPACE_ROOT`, `PSC_KNOWN_HOSTS`, etc.). No change is needed to existing PSC host-key entries.

### ACES settings

The normal and safest ACES access method is the official portal: https://portal-aces.hprc.tamu.edu

For advanced *direct ACES SSH access through the app* (only with approval from TAMU and your campus IT), set:

```toml
ACES_WORKSPACE_ROOT = "/scratch/user/{username}"
ACES_PARTITIONS = "cpu,gpu"
ACES_KNOWN_HOSTS = """
[aces-jump.hprc.tamu.edu]:8822 ssh-ed25519 REPLACE_WITH_VERIFIED_JUMP_HOST_PUBLIC_KEY
login.aces.hprc.tamu.edu ssh-ed25519 REPLACE_WITH_VERIFIED_LOGIN_HOST_PUBLIC_KEY
"""
```

These are **examples only**. The listed key types are placeholders; use exactly the algorithms and full host-key lines confirmed independently by TAMU HPRC. Never copy unverified `ssh-keyscan` output into a production app. A port-8822 entry must be written in OpenSSH known_hosts form `[hostname]:8822`. If your verified host uses another key algorithm, use that algorithm and value.

Login to the ACES portal, open **Utilities → sshca**, and download both `id_aces_tamu` (private key) and `id_aces_tamu-cert.pub` (signed public certificate). ACES documentation currently says the certificate lasts 49 hours. The app does not create, renew, or store certificates permanently. Upload both files when connecting through the app. The connection tunnels through `aces-jump.hprc.tamu.edu:8822` to `login.aces.hprc.tamu.edu:22`.

**Security:** Uploading a private key to Streamlit Community Cloud is sensitive and should not be enabled for students without express institutional/HPRC authorization. The files remain in Streamlit session memory for direct connections; they are not written to disk, cookies, or the server credential vault. A page reload means uploading the temporary credentials again. Closing/logging out removes them from Streamlit session state. The app server and Streamlit operators must still be trusted to handle secrets appropriately. A self-hosted server or authorized federation is preferable for routine class use.

Network access to port **8822** from Streamlit Community Cloud must be available, which has not been verified; the official portal is the reliable alternative.

### Features

- Select cluster at sign-in (Bridges-2 or ACES).
- File browser, editable path bar, uploads/downloads and folders.
- Molecular builder/viewer and cube surfaces.
- `.sh`, `.sbatch`, `.slurm`, `.job` submitted with `sbatch`, monitored via `squeue`/`sacct`.
- ACES uses per-user scratch root; other paths remain dependent on file permissions and optional `B2_ALLOW_OTHER_PATHS` setting.
- Cluster-specific partition selections; `Use script default` respects `#SBATCH --partition` in a script.

No scientific computation should be run on login nodes. Job submission only uses `sbatch`.

Sources: https://hprc.tamu.edu/kb/User-Guides/ACES/ and https://hprc.tamu.edu/kb/User-Guides/ACES/Batch/
