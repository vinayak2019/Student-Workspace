"""Generate student PySCF calculations in the current remote HPC folder.

No jobs run on Streamlit or on an HPC login node. Students submit the
resulting .sh using the existing Submit Simulation screen.
"""
import io
import json
import posixpath
import re
import shlex
import stat
from pathlib import Path

import streamlit as st

DRIVER = Path(__file__).resolve().parent / 'chem_sim.py'


def _slug(value):
    val = re.sub(r'[^A-Za-z0-9_-]+', '_', value.strip()).strip('_')
    if not val or len(val) > 60:
        raise ValueError('Choose a calculation name containing 1–60 letters, digits, _ or -.')
    return val


def _read_local_driver():
    if not DRIVER.is_file():
        raise RuntimeError('chem_sim.py is missing from the Streamlit repository. Add it alongside app.py.')
    return DRIVER.read_bytes()


def _shell_script(name, partition, hours, cpus, memory_gb, python_executable):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', partition):
        raise ValueError('Invalid SLURM partition.')
    if not python_executable.startswith('/') or '\n' in python_executable:
        raise ValueError('The compute Python executable must be an absolute path.')
    return f'''#!/bin/bash
#SBATCH --job-name={name[:40]}
#SBATCH --partition={partition}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={memory_gb}G
#SBATCH --time={hours:02d}:00:00
#SBATCH --output=slurm_%j.out
#SBATCH --error=slurm_%j.err
set -euo pipefail
cd "${{SLURM_SUBMIT_DIR:?Submit from calculation folder}}"
export OMP_NUM_THREADS="${{SLURM_CPUS_PER_TASK:-1}}"
export OPENBLAS_NUM_THREADS="$OMP_NUM_THREADS"
export MKL_NUM_THREADS="$OMP_NUM_THREADS"
PYTHON_BIN={shlex.quote(python_executable)}
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "PySCF environment missing: $PYTHON_BIN" >&2
    echo "Ask the instructor to install a shared compute-node Python environment." >&2
    exit 2
fi
exec "$PYTHON_BIN" chem_sim.py --config config.json
'''


def _write_exclusive(sftp, path, payload):
    with sftp.open(path, 'wx') as handle:
        handle.write(payload)


def _make_folder(sftp, folder, current, xyz_name, config, shell):
    """Create new remote folder and write every file; best effort cleanup on error.

    A unique folder and exclusive writes prevent overwriting student work.
    """
    sftp.mkdir(folder, mode=0o700)
    paths = []
    try:
        source = posixpath.join(current, xyz_name)
        dest = posixpath.join(folder, 'input.xyz')
        with sftp.open(source, 'rb') as original, sftp.open(dest, 'wx') as output:
            paths.append(dest)
            while True:
                chunk = original.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        if sftp.stat(dest).st_size > 4 * 1024 * 1024:
            raise ValueError('XYZ exceeds 4 MB. Use a smaller molecule for this teaching workflow.')
        for filename, payload in (
            ('config.json', json.dumps(config, indent=2).encode('utf-8')),
            ('chem_sim.py', _read_local_driver()),
            ('run_calculation.sh', shell.encode('utf-8')),
        ):
            target = posixpath.join(folder, filename)
            _write_exclusive(sftp, target, payload)
            paths.append(target)
    except Exception:
        for path in reversed(paths):
            try:
                sftp.remove(path)
            except OSError:
                pass
        try:
            sftp.rmdir(folder)
        except OSError:
            pass
        raise


def calculation_builder_page(sftp_connection, checked_remote_path, checked_new_path,
                             config_get, active_partitions, show_error, navigate):
    st.subheader('🧪 New Calculation')
    st.caption('Prepare a molecular DFT calculation, generate its Python configuration and SLURM .sh file, then submit from Actions → Submit Simulation. Calculations execute only on allocated compute nodes.')
    current = st.session_state.current
    try:
        ssh, sftp = sftp_connection()
        try:
            checked_remote_path(sftp, current)
            xyz_names = sorted(a.filename for a in sftp.listdir_attr(current)
                               if stat.S_ISREG(a.st_mode) and a.filename.lower().endswith('.xyz'))
        finally:
            sftp.close(); ssh.close()
    except Exception as exc:
        show_error(exc)
        return
    if not xyz_names:
        st.warning('No XYZ files in the current folder. Upload an XYZ or save one from Molecule Builder, then return here.')
        return
    if not active_partitions():
        st.error('No SLURM partitions are configured for this cluster.')
        return

    with st.form('new_calculation_form'):
        name = st.text_input('Calculation folder name', value='water_optimization',
                             help='A new folder will be created in the current location. Existing folders will not be overwritten.')
        xyz = st.selectbox('Molecule (XYZ in current folder)', xyz_names)
        mode_label = st.selectbox('Calculation type', [
            'Geometry optimization', 'Single-point energy / electron density',
            'Vibrational frequencies', 'Coordinate energy scan'])
        mode = {'Geometry optimization': 'optimize',
                'Single-point energy / electron density': 'density',
                'Vibrational frequencies': 'frequency',
                'Coordinate energy scan': 'scan'}[mode_label]
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            method = st.selectbox('Electronic method', ['PBE0', 'B3LYP', 'PBE', 'HF'])
        with c2:
            basis = st.selectbox('Basis set', ['def2-svp', 'def2-tzvp', 'sto-3g'])
        with c3:
            charge = st.number_input('Charge', value=0, min_value=-5, max_value=5, step=1)
        with c4:
            spin = st.number_input('2S (unpaired electrons)', value=0, min_value=0, max_value=10, step=1)
        orbitals = st.checkbox('Generate HOMO/LUMO cubes', value=(mode != 'scan'), disabled=mode == 'scan')
        density = st.checkbox('Generate electron-density cube', value=(mode != 'scan'), disabled=mode == 'scan')
        cube_grid = st.slider('Cube resolution (points per axis)', 24, 100, 60, disabled=mode == 'scan')
        opt_first = st.checkbox('Optimize before frequencies', value=True, disabled=mode != 'frequency')
        max_opt = st.number_input('Maximum optimization steps', 10, 300, 80, 10, disabled=mode not in ('frequency', 'optimize'))
        scan = None
        if mode == 'scan':
            kind = st.selectbox('Coordinate', ['distance', 'angle', 'dihedral'])
            needed = {'distance': 2, 'angle': 3, 'dihedral': 4}[kind]
            atoms_s = st.text_input('Atom indices (1-based, separated by commas)',
                                     value=','.join(str(x) for x in range(1, needed + 1)))
            moved_s = st.text_input('Atoms to move (1-based; includes the last selected atom)',
                                     value=str(needed), help='For dihedrals, include the terminal group atoms on the moving side. No constrained relaxation is performed.')
            s1, s2, s3 = st.columns(3)
            with s1:
                start = st.number_input('From (Å or degrees)', value=0.8 if kind == 'distance' else -180.0)
            with s2:
                stop = st.number_input('To (Å or degrees)', value=1.6 if kind == 'distance' else 180.0)
            with s3:
                steps = st.number_input('Points', min_value=2, max_value=101, value=9, step=1)
            scan = (kind, needed, atoms_s, moved_s, float(start), float(stop), int(steps))
        st.markdown('**ACES / SLURM resources**')
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            partition = st.selectbox('Partition', active_partitions())
        with r2:
            hours = st.number_input('Wall time (hours)', min_value=1, max_value=48, value=4)
        with r3:
            cpus = st.number_input('CPU cores', min_value=1, max_value=32, value=4)
        with r4:
            memory = st.number_input('Memory (GB)', min_value=1, max_value=128, value=12)
        python_executable = st.text_input('Shared compute-node Python executable',
            value=config_get('ACES_CHEM_PYTHON' if st.session_state.get('cluster') == 'aces' else 'B2_CHEM_PYTHON',
                             '/scratch/group/p.che230117.000/chemistry-venv/bin/python' if st.session_state.get('cluster') == 'aces' else '/ocean/projects/PROJECT_ID/shared/chemistry-venv/bin/python'),
            help='Must be a preinstalled PySCF + geomeTRIC + ASE environment accessible on compute nodes.')
        create = st.form_submit_button('Create calculation files', type='primary')

    if create:
        try:
            slug = _slug(name)
            spec = {'task':mode, 'xyz':'input.xyz', 'output':'results', 'method':method,
                    'basis':basis, 'charge':int(charge), 'spin':int(spin),
                    'orbitals':bool(orbitals) and mode != 'scan',
                    'electron_density_cube':bool(density) and mode != 'scan',
                    'cube_grid':int(cube_grid), 'max_opt_steps':int(max_opt)}
            if mode == 'frequency':
                spec.update(optimize_first=bool(opt_first), vibration_frames=16,
                            vibration_amplitude_angstrom=0.25)
            if scan:
                kind, needed, atom_text, moved_text, start, stop, steps = scan
                atom_indices = [int(x.strip()) for x in atom_text.split(',') if x.strip()]
                moved_indices = [int(x.strip()) for x in moved_text.split(',') if x.strip()]
                if len(atom_indices) != needed or len(set(atom_indices)) != needed or min(atom_indices) < 1:
                    raise ValueError(f'{kind} scan needs {needed} distinct positive atom indices.')
                if not moved_indices or min(moved_indices) < 1 or atom_indices[-1] not in moved_indices:
                    raise ValueError('Moving atoms must include the final coordinate atom.')
                if kind == 'dihedral' and set(atom_indices[:3]).intersection(moved_indices):
                    raise ValueError('For dihedral scans, do not move the first three selected atoms.')
                if start == stop or (kind == 'distance' and min(start,stop) <= 0) or (kind == 'angle' and not (0 < min(start,stop) < max(start,stop) < 180)):
                    raise ValueError('Invalid coordinate range for this scan.')
                spec['scan'] = {'coordinate':kind, 'atoms':atom_indices, 'moved_atoms':moved_indices,
                                'start':start, 'stop':stop, 'steps':steps}
            script = _shell_script(slug, partition, int(hours), int(cpus), int(memory), python_executable)
            folder = posixpath.join(current, slug)
            ssh, sftp = sftp_connection()
            try:
                checked_new_path(sftp, folder)
                try:
                    sftp.lstat(folder)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('That calculation folder already exists. Choose a different name.')
                _make_folder(sftp, folder, current, xyz, spec, script)
            finally:
                sftp.close(); ssh.close()
            st.success(f'Created {slug}/input.xyz, config.json, chem_sim.py and run_calculation.sh. Nothing has been submitted yet.')
            st.session_state['calculation_created'] = folder
        except Exception as exc:
            show_error(exc)

    if folder := st.session_state.get('calculation_created'):
        st.code(folder)
        if st.button('Open calculation folder and choose Submit Simulation'):
            st.session_state['calculation_created'] = None
            st.session_state.view = 'File Explorer'
            navigate(folder)
