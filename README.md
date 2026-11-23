# HPC Student Workspace — Bridges-2 + ACES

A Windows-inspired browser file manager, molecular editor/viewer, and SLURM job manager for PSC Bridges-2 and Texas A&M ACES.

## Deploy to Streamlit Community Cloud

Commit `app.py`, `aces_connection.py`, `molecule_viewer.py`, `molecule_builder.py`, and `requirements.txt` to the same GitHub directory. Launch using `app.py` as the entrypoint.

### Bridges-2 settings

Keep your previously configured Streamlit Secrets (`B2_WORKSPACE_ROOT`, `PSC_KNOWN_HOSTS`, etc.). No change is needed to existing PSC host-key entries.

### ACES settings

To override the configured workspace path in Streamlit Cloud, update `ACES_WORKSPACE_ROOT` in App Settings → Secrets. **An existing Secrets value overrides the new code default.**

The normal and safest ACES access method is the official portal: https://portal-aces.hprc.tamu.edu

For advanced *direct ACES SSH access through the app* (only with approval from TAMU and your campus IT), set:

```toml
ACES_WORKSPACE_ROOT = "/scratch/group/p.che230117.000/{username}"
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
- ACES defaults to `/scratch/group/p.che230117.000/{username}`. On login, the app creates only the student's own folder if missing, with a requested mode of `0700`. The group directory must already exist and grant the user permission to create it; a missing parent or denied permission results in an explicit error. Existing folders are preserved. Other locations remain subject to their own account permissions and optional `B2_ALLOW_OTHER_PATHS` setting.
- Because ACES scratch is **not backed up**, students must archive important outputs elsewhere.
- Cluster-specific partition selections; `Use script default` respects `#SBATCH --partition` in a script.

No scientific computation should be run on login nodes. Job submission only uses `sbatch`.

Sources: https://hprc.tamu.edu/kb/User-Guides/ACES/ and https://hprc.tamu.edu/kb/User-Guides/ACES/Batch/

## New Calculation: build, then submit

The **Actions → New Calculation** screen creates an independent calculation folder inside the current remote folder. Start by uploading an XYZ file (or generating one with Molecule Builder). Select a supported isolated-molecule task, method, basis, charge, spin, HOMO/LUMO and electron-density cube options, and optional geometry scan coordinates.

After clicking **Create calculation files**, open the generated folder and choose **Actions → Submit Simulation**, selecting `run_calculation.sh`. The app creates these four files without submitting the job automatically:

- `input.xyz`: copy of the student's starting geometry
- `config.json`: PySCF calculation settings
- `chem_sim.py`: simulation driver shipped with the Streamlit app
- `run_calculation.sh`: SLURM script with selected CPU, memory, partition and wall time

Result files appear under `results/` within the calculation folder. Optimization and density jobs can generate `electron_density.cube` and frontier orbital `.cube` files for the existing Streamlit 3D viewer. Frequency runs generate CSV frequencies and multi-frame `.xyz` mode trajectories (the viewer may show only the first frame). Scans generate `scan_energies.csv` and `scan_structures.xyz`; they are **unrelaxed, rigid scans**.

**Before student use:** Install PySCF, NumPy, SciPy, geomeTRIC and ASE in a Python environment accessible from ACES compute nodes. Do installation only from a compute-node allocation, not an HPC login node. Configure the absolute path to that environment's Python executable in Streamlit Secrets:

```toml
ACES_CHEM_PYTHON = "/scratch/group/p.che230117.000/chemistry-venv/bin/python"
```

The path above is an example and **does not mean that environment already exists**. Set an actual working path. Use `B2_CHEM_PYTHON` for Bridges-2. A job fails with an actionable log message if the environment is missing. Partitions come from existing `ACES_PARTITIONS`/`B2_PARTITIONS` Secrets; verify they are suitable for your allocation. No scientific calculations run on Streamlit Cloud or login nodes.

The current package has syntax tests only; live PySCF/SLURM execution and file creation still require testing on ACES.
