# Bridges-2 Student Workspace (Streamlit MVP)

A teaching-focused front end to PSC Bridges-2. The app logs in as the **student's own PSC identity**. It does not create PSC accounts, bypass authentication, or grant directory permissions.

## Included

- Per-student login through SSH password authentication (if allowed by PSC)
- Rooted project file explorer, mkdir, text preview, uploads and downloads
- Separate SSH (login host) and SFTP (PSC designated DTN) connections
- Submission of existing SLURM batch scripts, selecting partition and optional allocation
- `squeue` current jobs, `sacct` 7-day history and job details
- Read text logs in the selected folder

## Configure

1. Confirm deployment, credential-handling, authentication, and transfer method with PSC and your institution's IT/security team. OnDemand at https://ondemand.bridges2.psc.edu is the PSC-supported alternative.
2. Every student needs a PSC account, valid project membership and access to a workspace path. Set `B2_WORKSPACE_ROOT` to their **actual** accessible path. Pattern `/ocean/projects/YOUR_PROJECT_GROUP/{username}` is only an example; it does not create directories. If you want students restricted to a shared course folder, configure one appropriately, and use Unix permissions to protect student data. This app's path restriction is a UI convenience, **not** a substitute for OS permissions.
3. On the server running Streamlit, establish and verify the SSH host keys of both `bridges2.psc.edu` and `data.bridges2.psc.edu` out-of-band against trustworthy PSC fingerprints. Populate `~/.ssh/known_hosts`. The app rejects unknown/changed host keys. Never automatically trust keys received on first connection.
4. Use Python 3.10+, install dependencies: `pip install -r requirements.txt`.
5. Export the environment variables from `.env.example` using your deployment secrets/config (Streamlit does not automatically load `.env`). **Replace** `YOUR_PROJECT_GROUP` and check partition access.
6. Run `streamlit run app.py` on your own approved web server behind HTTPS, authentication/network controls, and a reverse proxy configured to prevent caching and request logging of secrets. Avoid a public multi-tenant community-hosted instance.
7. Set server and reverse proxy upload sizes to match `B2_MAX_UPLOAD_MB`; Streamlit defaults may differ.

## Security and limitations

**MVP, not production hardened.** Student PSC passwords are kept in Streamlit session memory for repeated SSH/SFTP requests; they are not saved to local files or a database. This has residual security risks (server operators/process memory, long-lived sessions, compromises, and browser/websocket exposure). Require HTTPS and session timeout, obtain PSC approval, and replace password handling with an approved federated or delegated mechanism for production. Some PSC auth flows (including interactive challenge/MFA) may not work with Paramiko password login. Never ask students to share credentials with instructors. PSC policies govern account usage and workload submissions.

The application makes remote shell calls using fixed commands and shell quoting, not user-submitted shell strings. SFTP path normalization checks help prevent following symlinks outside the configured directory. System permissions remain authoritative. Review upload overwrite policy before deployment: currently uploading a same-named file overwrites it. Restrict server access, use session-timeout and audit logging excluding secrets, and test simultaneous student sessions. This MVP downloads files into app memory, limited by configuration; use PSC's approved transfer tooling for large files. The `squeue`/`sacct` views are **per user** and the accounting history is limited to seven days in the interface; historical records and actual log retention are governed by PSC. Logs are discovered in the current folder only.

The script's chosen partition is passed to `sbatch` and can override the partition inside the script. SBATCH allocation ID remains optional. Users must verify partitions, walltimes and allocation charging. This app does not provide a simulation-template builder, a file editor, global log search, scheduler cancellation, or desktop windows; those are appropriate next versions.

**Important**: PSC says transfers should use `data.bridges2.psc.edu` and be initiated from a machine outside the login nodes. Deploy this web app **outside Bridges-2 login nodes**. Do not run compute-intensive analyses on the Streamlit server or Bridges-2 login nodes.

Documentation: https://www.psc.edu/resources/bridges-2/user-guide/


## Streamlit Community Cloud (`share.streamlit.io`) configuration

Deploy `app.py` from your GitHub repository. Open the app's **Settings → Secrets** and add:

```toml
B2_WORKSPACE_ROOT = "/ocean/projects/YOUR_PROJECT_GROUP/{username}"
B2_LOGIN_HOST = "bridges2.psc.edu"
B2_DATA_HOST = "data.bridges2.psc.edu"
B2_PARTITIONS = "RM-shared,RM"
PSC_KNOWN_HOSTS = """
bridges2.psc.edu ssh-ed25519 REPLACE_WITH_VERIFIED_BASE64_PUBLIC_HOST_KEY
 data.bridges2.psc.edu ssh-ed25519 REPLACE_WITH_VERIFIED_BASE64_PUBLIC_HOST_KEY
"""
```

Remove the leading space before `data.bridges2.psc.edu` if copying the template.
**The placeholders are not valid keys.** Obtain candidate host-key lines using
`ssh-keyscan -t ed25519 bridges2.psc.edu data.bridges2.psc.edu` from a trusted
machine; independently confirm the SHA256 fingerprints with PSC support before
putting those lines in secrets. Never paste usernames, passwords, or private SSH
keys into GitHub. Restart/reboot the app after saving Secrets.

This release refuses unknown or mismatching SSH host keys. Network egress on
TCP port 22 and PSC's SSH login policy must also allow Streamlit Community Cloud
connections. Not every PSC authentication method works through password-only
Paramiko; this is an MVP and **is not suitable for public class-wide deployment
until the authentication/security model is approved by PSC and campus IT**.

## Desktop-style File Explorer

The updated File Explorer has folder tiles, direct click-to-open navigation,
project breadcrumbs, grid/list toggle, a folder-name filter, multi-file upload,
folder creation, file preview, download, rename, and confirmation-gated file deletion.
These operations are performed over SFTP against PSC's data-transfer host and
are restricted to the configured root. A folder opens on one click rather than
requiring a browser double-click. Folder deletion and drag-and-drop folder moves
are intentionally not included.

**Cloud update:** Replace `app.py` in your GitHub repository, retain the existing
Streamlit secrets (especially the *verified* `PSC_KNOWN_HOSTS` entries), then
reboot the application in share.streamlit.io. Python package requirements are
unchanged. The application has not been integration-tested against a live PSC account.


### File icon display

Use the **Icon size** dropdown in the Files toolbar to select Small, Medium, Large, or Extra large. Large is the default. File sizes are not displayed in the explorer grid, list, or selected-file details.

## 3D molecular viewer

The Files screen now offers **Open 3D molecule viewer** for `.pdb`, `.sdf`, `.cif`, `.mol2`, and `.xyz`. The viewer supports ball-and-stick, sticks, space-filling, lines, and protein cartoon display; white/dark/light-blue backgrounds; automatic rotation; and optional CIF unit-cell display when the structure provides the relevant information. Rotate by dragging, zoom with the scroll wheel, and pan with right-drag.

**Deploy both `app.py` and `molecule_viewer.py` together** in the same GitHub folder. No new pip dependencies are needed: the viewer loads 3Dmol.js from jsDelivr in the student's browser. The browser therefore needs external HTTPS access to that CDN. Preview is limited to 8 MB per molecular file to protect web-app memory; large trajectories should be downloaded and viewed locally. This is visualization, not a chemically validated structure conversion, and guessed bonds (especially XYZ) and CIF unit-cell/symmetry representations should be checked before chemical interpretation. Files are fetched through the existing authenticated SFTP session; the viewer does not send them to a structure database.

## Molecule Builder (2D to 3D)

Choose **Actions → Molecule builder**. Draw a small organic molecule in the Ketcher 2D editor, click **Generate 3D molecule**, inspect the result in the 3Dmol.js viewer, and download XYZ/SDF or **Save in current folder** on Bridges-2. SDF retains bond orders/connectivity; XYZ contains coordinates and element labels only. RDKit generates one 3D conformer with ETKDG and optionally minimizes using MMFF94 (fallback UFF). This does **not** execute on Bridges-2 or submit a SLURM simulation. Force-field energies are not quantum energies and are not suitable for direct comparison across unlike molecules. Complex, metallic, periodic, flexible, or highly strained structures might fail or need scientific review. Generated structures include explicit hydrogens.

**Deployment:** Add `molecule_builder.py` to the same GitHub directory as `app.py` and `molecule_viewer.py`. Commit the included `requirements.txt` with `streamlit-ketcher` and `rdkit`; Streamlit Community Cloud will install these when redeployed. Your PSC Secrets remain unchanged. The 2D component is served by the Streamlit app; the 3D viewer uses 3Dmol.js via a browser CDN. No changes to PSC directories or user privileges are required.

**Security:** The editor and quick optimization run on the Streamlit hosting instance, not a Bridges-2 compute node. The builder caps molecular size. Server-side PSC credential handling remains an MVP and should be approved by PSC and campus IT before classroom deployment.
