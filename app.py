import io
import os
import posixpath
import re
import shlex
import stat
from datetime import datetime

import paramiko
import streamlit as st

st.set_page_config(page_title='Bridges-2 Student Workspace', page_icon='🧪', layout='wide')

LOGIN_HOST = os.getenv('B2_LOGIN_HOST', 'bridges2.psc.edu')
DATA_HOST = os.getenv('B2_DATA_HOST', 'data.bridges2.psc.edu')
ROOT_TEMPLATE = os.getenv('B2_WORKSPACE_ROOT', '/ocean/projects/PROJECT_ID/{username}')
KNOWN_HOSTS = os.getenv('B2_KNOWN_HOSTS', os.path.expanduser('~/.ssh/known_hosts'))
MAX_UPLOAD_MB = int(os.getenv('B2_MAX_UPLOAD_MB', '100'))
ALLOWED_PARTITIONS = [x.strip() for x in os.getenv('B2_PARTITIONS', 'RM-shared,RM').split(',') if x.strip()]
MAX_DOWNLOAD_MB = int(os.getenv('B2_MAX_DOWNLOAD_MB', '100'))

st.markdown('''<style>
.block-container {padding-top: 1.4rem;}
[data-testid="stMetric"] {background: #f6f8fb; border-radius: 12px; padding: 10px;}
</style>''', unsafe_allow_html=True)


def connect(host, username, password):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if os.path.isfile(KNOWN_HOSTS):
        client.load_host_keys(KNOWN_HOSTS)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(hostname=host, username=username, password=password,
                   look_for_keys=False, allow_agent=False, timeout=15,
                   auth_timeout=25, banner_timeout=20)
    return client


def ssh_command(args, cwd=None):
    ssh = connect(LOGIN_HOST, st.session_state.username, st.session_state.password)
    try:
        cmd = ' '.join(shlex.quote(str(a)) for a in args)
        if cwd is not None:
            cmd = f'cd {shlex.quote(cwd)} && {cmd}'
        _, stdout, stderr = ssh.exec_command(cmd, timeout=25)
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(error.strip() or output.strip() or f'Command failed ({rc})')
        return output.strip()
    finally:
        ssh.close()


def sftp_connection():
    ssh = connect(DATA_HOST, st.session_state.username, st.session_state.password)
    try:
        sftp = ssh.open_sftp()
        return ssh, sftp
    except Exception:
        ssh.close()
        raise


def ensure_within_root(path):
    root = st.session_state.root
    candidate = posixpath.normpath(path)
    if candidate != root and not candidate.startswith(root.rstrip('/') + '/'):
        raise ValueError('Outside the configured workspace.')
    return candidate


def safe_child(parent, name):
    if not name or name in ('.', '..') or '/' in name or '\\' in name or '\x00' in name:
        raise ValueError('Please enter a simple file or folder name.')
    return ensure_within_root(posixpath.join(parent, name))


def checked_remote_path(sftp, path):
    '''Resolve remote symlinks; refuse escapes, including symlinked parents.'''
    path = ensure_within_root(path)
    resolved = sftp.normalize(path)
    root_real = sftp.normalize(st.session_state.root)
    if resolved != root_real and not resolved.startswith(root_real.rstrip('/') + '/'):
        raise ValueError('A link points outside the configured workspace.')
    return path


def checked_new_path(sftp, path):
    ensure_within_root(path)
    checked_remote_path(sftp, posixpath.dirname(path))
    return path


def show_error(exc):
    st.error(f'Operation failed: {exc}')


def editor_preview(path):
    ssh, sftp = sftp_connection()
    try:
        checked_remote_path(sftp, path)
        attributes = sftp.stat(path)
        if attributes.st_size > 256 * 1024:
            st.info('Preview limited to files smaller than 256 KB.')
            return
        with sftp.open(path, 'rb') as f:
            raw = f.read(256 * 1024 + 1)
        if b'\x00' in raw:
            st.info('Binary file; download to inspect.')
            return
        st.code(raw.decode('utf-8', errors='replace'), language=None)
    finally:
        sftp.close()
        ssh.close()


def file_manager():
    st.subheader('📁 File Explorer')
    current = st.session_state.current
    a, b, c = st.columns([1, 6, 2])
    with a:
        if st.button('⬆ Parent', disabled=(current == st.session_state.root)):
            st.session_state.current = ensure_within_root(posixpath.dirname(current))
            st.rerun()
    with b:
        st.code(current)
    with c:
        if st.button('↻ Refresh'):
            st.rerun()

    try:
        ssh, sftp = sftp_connection()
        try:
            checked_remote_path(sftp, current)
            attrs = sftp.listdir_attr(current)
            attrs.sort(key=lambda x: (not stat.S_ISDIR(x.st_mode), x.filename.casefold()))
            rows = []
            for a in attrs:
                kind = 'Folder' if stat.S_ISDIR(a.st_mode) else ('Link' if stat.S_ISLNK(a.st_mode) else 'File')
                rows.append({'Name': a.filename, 'Type': kind, 'Size (KB)': '' if kind == 'Folder' else round(a.st_size / 1024, 1), 'Modified': datetime.fromtimestamp(a.st_mtime).strftime('%Y-%m-%d %H:%M')})
            st.dataframe(rows, use_container_width=True, hide_index=True)
            dirs = [a.filename for a in attrs if stat.S_ISDIR(a.st_mode)]
            regular = [a.filename for a in attrs if stat.S_ISREG(a.st_mode)]
        finally:
            sftp.close()
            ssh.close()
    except Exception as exc:
        show_error(exc)
        return

    left, right = st.columns(2)
    with left:
        if dirs:
            chosen_dir = st.selectbox('Open folder', dirs, key='folder_select')
            if st.button('Open selected folder'):
                try:
                    candidate = safe_child(current, chosen_dir)
                    ssh, sftp = sftp_connection()
                    try:
                        checked_remote_path(sftp, candidate)
                    finally:
                        sftp.close(); ssh.close()
                    st.session_state.current = candidate
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
        with st.form('mkdir_form', clear_on_submit=True):
            folder = st.text_input('New folder name')
            if st.form_submit_button('Create folder'):
                try:
                    path = safe_child(current, folder)
                    ssh, sftp = sftp_connection()
                    try:
                        checked_new_path(sftp, path)
                        sftp.mkdir(path)
                    finally:
                        sftp.close(); ssh.close()
                    st.success('Folder created.'); st.rerun()
                except Exception as exc:
                    show_error(exc)
    with right:
        upload = st.file_uploader('Upload a file', key=f'upload_{current}')
        if upload and st.button('Upload to current folder'):
            if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
                st.error(f'File exceeds {MAX_UPLOAD_MB} MB limit.')
            else:
                try:
                    path = safe_child(current, upload.name)
                    ssh, sftp = sftp_connection()
                    try:
                        checked_new_path(sftp, path)
                        with sftp.open(path, 'wb') as target:
                            while chunk := upload.read(1024 * 1024):
                                target.write(chunk)
                    finally:
                        sftp.close(); ssh.close()
                    st.success('Uploaded.'); st.rerun()
                except Exception as exc:
                    show_error(exc)

    if regular:
        selected_file = st.selectbox('Select file', regular)
        file_path = safe_child(current, selected_file)
        f1, f2 = st.columns(2)
        with f1:
            if st.button('Preview selected file'):
                try:
                    editor_preview(file_path)
                except Exception as exc:
                    show_error(exc)
        with f2:
            try:
                ssh, sftp = sftp_connection()
                try:
                    checked_remote_path(sftp, file_path)
                    size = sftp.stat(file_path).st_size
                    if size <= MAX_DOWNLOAD_MB * 1024 * 1024:
                        with sftp.open(file_path, 'rb') as f:
                            data = f.read()
                    else:
                        data = None
                finally:
                    sftp.close(); ssh.close()
                if data is None:
                    st.warning(f'Download limit: {MAX_DOWNLOAD_MB} MB. Use PSC transfer tools for larger data.')
                else:
                    st.download_button('Download selected file', data=data, file_name=selected_file)
            except Exception as exc:
                show_error(exc)


def submit_job():
    st.subheader('🚀 Submit Simulation')
    st.caption('Choose a previously prepared .sbatch, .slurm or .job script. Jobs run on compute nodes under your PSC allocation.')
    try:
        ssh, sftp = sftp_connection()
        try:
            checked_remote_path(sftp, st.session_state.current)
            names = [a.filename for a in sftp.listdir_attr(st.session_state.current)
                     if stat.S_ISREG(a.st_mode) and a.filename.endswith(('.sbatch', '.slurm', '.job'))]
        finally:
            sftp.close(); ssh.close()
    except Exception as exc:
        show_error(exc); return
    if not names:
        st.info('Upload a .sbatch, .slurm, or .job script to the current folder using File Explorer first.')
        return
    with st.form('submit_form'):
        script = st.selectbox('Job script', names)
        partition = st.selectbox('Partition', ALLOWED_PARTITIONS)
        allocation = st.text_input('Allocation ID (optional)', help='Use the SLURM allocation granted by PSC, if required.')
        submit = st.form_submit_button('Submit job')
    if submit:
        if allocation and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', allocation):
            st.error('Invalid allocation ID.'); return
        try:
            path = safe_child(st.session_state.current, script)
            ssh, sftp = sftp_connection()
            try:
                checked_remote_path(sftp, path)
            finally:
                sftp.close(); ssh.close()
            cmd = ['sbatch', '--parsable', '--partition', partition]
            if allocation:
                cmd += ['--account', allocation]
            cmd += ['--', script]
            result = ssh_command(cmd, cwd=st.session_state.current)
            st.success(f'Job submitted. SLURM response: {result}')
        except Exception as exc:
            show_error(exc)


def jobs_page():
    st.subheader('📊 Job Monitor')
    if st.button('Refresh jobs'):
        st.rerun()
    try:
        pending = ssh_command(['squeue', '--noheader', '--me', '--format=%i|%j|%T|%P|%M|%D'])
        live = [dict(zip(['Job ID', 'Name', 'State', 'Partition', 'Elapsed', 'Nodes'], row.split('|', 5))) for row in pending.splitlines() if row.strip()]
        st.markdown('**Queued and running jobs**')
        st.dataframe(live, use_container_width=True, hide_index=True)
    except Exception as exc:
        show_error(exc)
    st.markdown('**Recent job history (SLURM accounting)**')
    try:
        history = ssh_command(['sacct', '-u', st.session_state.username, '--starttime', 'now-7days', '--noheader', '--parsable2', '--format=JobID,JobName,Partition,State,Elapsed,ExitCode', '-X'])
        rows = [dict(zip(['Job ID', 'Name', 'Partition', 'State', 'Elapsed', 'Exit code'], row.split('|', 5))) for row in history.splitlines() if row.strip()]
        st.dataframe(rows[:300], use_container_width=True, hide_index=True)
    except Exception as exc:
        show_error(exc)
    st.caption('History depends on SLURM accounting retention. Log files remain in your project folders when produced by the job.')
    with st.expander('Inspect job details'):
        job_id = st.text_input('Numeric SLURM Job ID')
        if st.button('View job details'):
            if not re.fullmatch(r'\d+(?:_[0-9]+)?', job_id):
                st.error('Enter a valid Job ID.')
            else:
                try:
                    st.code(ssh_command(['sacct', '-j', job_id, '--format=JobID,JobName,State,Elapsed,ExitCode,StdOut,StdErr', '-P']))
                except Exception as exc:
                    show_error(exc)


def logs_page():
    st.subheader('📄 Job Logs')
    st.caption('Browse output and error files in your current workspace directory; navigate folders in File Explorer.')
    try:
        ssh, sftp = sftp_connection()
        try:
            checked_remote_path(sftp, st.session_state.current)
            matches = [a.filename for a in sftp.listdir_attr(st.session_state.current)
                       if stat.S_ISREG(a.st_mode) and (a.filename.endswith(('.out', '.err', '.log')) or a.filename.startswith('slurm-'))]
        finally:
            sftp.close(); ssh.close()
        if not matches:
            st.info('No .out, .err, .log or slurm-* files in this folder.'); return
        chosen = st.selectbox('Select output or error log', sorted(matches))
        if st.button('Read log'):
            path = safe_child(st.session_state.current, chosen)
            ssh, sftp = sftp_connection()
            try:
                checked_remote_path(sftp, path)
                size = sftp.stat(path).st_size
                with sftp.open(path, 'rb') as f:
                    if size > 128 * 1024:
                        f.seek(size - 128 * 1024)
                    content = f.read().decode('utf-8', errors='replace')
                st.code(content, language='text')
                if size > 128 * 1024:
                    st.caption('Showing final 128 KB of the log.')
            finally:
                sftp.close(); ssh.close()
    except Exception as exc:
        show_error(exc)


def login_screen():
    st.title('🧪 Bridges-2 Student Workspace')
    st.write('Sign in with your own PSC account. Credentials are held only in this Streamlit session; deploy only over HTTPS on an access-controlled server.')
    if 'PROJECT_ID' in ROOT_TEMPLATE:
        st.warning('Administrator: configure B2_WORKSPACE_ROOT before enabling student access.')
        st.stop()
    with st.form('login'):
        username = st.text_input('PSC username')
        password = st.text_input('PSC password', type='password')
        submitted = st.form_submit_button('Connect to Bridges-2')
    if submitted:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
            st.error('Enter a valid PSC username.'); return
        root = posixpath.normpath(ROOT_TEMPLATE.format(username=username))
        if not root.startswith('/') or root == '/':
            st.error('Invalid configured workspace.'); return
        try:
            client = connect(LOGIN_HOST, username, password)
            client.close()
            client, sftp = connect(DATA_HOST, username, password), None
            try:
                sftp = client.open_sftp()
                root = sftp.normalize(root)
                if not stat.S_ISDIR(sftp.stat(root).st_mode):
                    raise ValueError('Project path is not a directory.')
            finally:
                if sftp: sftp.close()
                client.close()
            st.session_state.username = username
            st.session_state.password = password
            st.session_state.root = root
            st.session_state.current = root
            st.rerun()
        except Exception as exc:
            show_error(exc)
    st.caption('PSC may require additional authentication or a supported access method. If password authentication is unavailable, use PSC Open OnDemand while an approved SSH authentication integration is configured.')


if 'username' not in st.session_state:
    login_screen()
    st.stop()

with st.sidebar:
    st.title('🧪 Bridges-2')
    st.write(f'User: **{st.session_state.username}**')
    view = st.radio('Workspace', ['File Explorer', 'Submit Simulation', 'Job Monitor', 'Job Logs', 'Account'])
    st.divider()
    if st.button('Log out'):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

if view == 'File Explorer':
    file_manager()
elif view == 'Submit Simulation':
    submit_job()
elif view == 'Job Monitor':
    jobs_page()
elif view == 'Job Logs':
    logs_page()
else:
    st.subheader('⚙️ Workspace')
    st.write('PSC username:', st.session_state.username)
    st.write('Login host:', LOGIN_HOST)
    st.write('Data transfer host:', DATA_HOST)
    st.code(st.session_state.root)
    if st.button('View my Unix groups'):
        try:
            st.code(ssh_command(['id', '-Gn']))
        except Exception as exc:
            show_error(exc)
