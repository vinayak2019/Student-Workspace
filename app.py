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

def config(name, default=None):
    """Read Streamlit Community Cloud Secrets, then environment, then default."""
    try:
        value = st.secrets.get(name)
        if value is not None:
            return str(value)
    except (FileNotFoundError, KeyError):
        pass
    return os.getenv(name, default)


LOGIN_HOST = config('B2_LOGIN_HOST', 'bridges2.psc.edu')
DATA_HOST = config('B2_DATA_HOST', 'data.bridges2.psc.edu')
ROOT_TEMPLATE = config('B2_WORKSPACE_ROOT', '/ocean/projects/PROJECT_ID/{username}')
KNOWN_HOSTS = config('B2_KNOWN_HOSTS', os.path.expanduser('~/.ssh/known_hosts'))
MAX_UPLOAD_MB = int(config('B2_MAX_UPLOAD_MB', '100'))
ALLOWED_PARTITIONS = [x.strip() for x in config('B2_PARTITIONS', 'RM-shared,RM').split(',') if x.strip()]
MAX_DOWNLOAD_MB = int(config('B2_MAX_DOWNLOAD_MB', '100'))


def load_verified_host_keys(client):
    """Use PSC_KNOWN_HOSTS from Cloud Secrets; fail closed if absent or malformed.

    Host keys MUST be independently verified with PSC before adding to Secrets.
    """
    client.load_system_host_keys()
    keys_text = config('PSC_KNOWN_HOSTS', '')
    if keys_text and keys_text.strip():
        keys = paramiko.HostKeys()
        for line_number, raw in enumerate(keys_text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            try:
                entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
                if entry is None or entry.key is None or not entry.hostnames:
                    raise ValueError('invalid key entry')
                for hostname in entry.hostnames:
                    keys.add(hostname, entry.key.get_name(), entry.key)
            except Exception as exc:
                raise RuntimeError(
                    f'PSC_KNOWN_HOSTS line {line_number} is invalid. Each line must contain '
                    'a hostname, a key type (such as ssh-ed25519), and the complete '
                    'Base64 public host key. Remove example placeholders and fingerprints; '
                    'use only independently verified PSC host keys.'
                ) from exc
        for host in (LOGIN_HOST, DATA_HOST):
            if keys.lookup(host) is None:
                raise RuntimeError(f'PSC_KNOWN_HOSTS is missing a verified key for {host}.')
        for hostname, types in keys.items():
            for algorithm, key in types.items():
                client.get_host_keys().add(hostname, algorithm, key)
    elif os.path.isfile(KNOWN_HOSTS):
        client.load_host_keys(KNOWN_HOSTS)
    else:
        raise RuntimeError(
            'SSH trust not configured. In share.streamlit.io open App Settings → Secrets '
            'and add PSC_KNOWN_HOSTS containing the independently verified SSH host keys '
            'for both bridges2.psc.edu and data.bridges2.psc.edu.'
        )
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


st.markdown('''<style>
.block-container {padding-top: 1.4rem;}
[data-testid="stMetric"] {background: #f6f8fb; border-radius: 12px; padding: 10px;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 12px;}
.stButton button {min-height: 2.6rem;}
</style>''', unsafe_allow_html=True)


def connect(host, username, password):
    client = paramiko.SSHClient()
    try:
        load_verified_host_keys(client)
        client.connect(hostname=host, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=15,
                       auth_timeout=25, banner_timeout=20)
        return client
    except Exception:
        client.close()
        raise


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


def _human_size(size):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def _navigate(path):
    st.session_state.current = path
    st.session_state.selected_item = None
    st.rerun()


def _file_tile(entry, current, index):
    name = entry.filename
    directory = stat.S_ISDIR(entry.st_mode)
    symlink = stat.S_ISLNK(entry.st_mode)
    selected = st.session_state.get('selected_item') == name
    icon = '📁' if directory else ('🔗' if symlink else ('📜' if name.endswith(('.out', '.err', '.log')) else '📄'))
    if name.endswith(('.sbatch', '.slurm', '.job')) and not directory:
        icon = '🚀'
    # Native Streamlit buttons keep event handling on the server (no injected JS).
    if st.button(f'{icon}  {name}', key=f'tile_{current}_{index}',
                 use_container_width=True, type='primary' if selected else 'secondary',
                 help='Open folder' if directory else 'Select file to see actions'):
        try:
            target = safe_child(current, name)
            if directory:
                ssh, sftp = sftp_connection()
                try:
                    checked_remote_path(sftp, target)
                finally:
                    sftp.close(); ssh.close()
                _navigate(target)
            else:
                st.session_state.selected_item = name
                st.rerun()
        except Exception as exc:
            show_error(exc)
    st.caption('Folder' if directory else ('Symbolic link' if symlink else _human_size(entry.st_size)))


def file_manager():
    st.title('🗂️ My Bridges-2 Files')
    current = st.session_state.current
    root = st.session_state.root
    rel = posixpath.relpath(current, root)
    crumbs = [] if rel == '.' else rel.split('/')
    st.caption('File explorer · Files remain on PSC Bridges-2 · Click a folder to open; click a file to select it')

    toolbar = st.columns([1, 1, 1, 1, 4])
    with toolbar[0]:
        if st.button('⬆ Up', disabled=current == root, use_container_width=True):
            _navigate(ensure_within_root(posixpath.dirname(current)))
    with toolbar[1]:
        if st.button('🏠 Home', use_container_width=True):
            _navigate(root)
    with toolbar[2]:
        if st.button('🔄 Reload', use_container_width=True):
            st.rerun()
    with toolbar[3]:
        st.toggle('📋 List', key='explorer_list', help='Switch between grid tiles and a file list')
    with toolbar[4]:
        st.text_input('Current remote directory', value=current, disabled=True, label_visibility='collapsed')

    # Clickable breadcrumbs never expose filesystem locations outside the project root.
    breadcrumb_cols = st.columns(min(8, len(crumbs) + 1))
    with breadcrumb_cols[0]:
        if st.button('🏠 Project', key='crumb_root'):
            _navigate(root)
    for i, part in enumerate(crumbs[:7]):
        with breadcrumb_cols[min(i+1, len(breadcrumb_cols)-1)]:
            if st.button(f'› {part}', key=f'crumb_{i}'):
                _navigate(ensure_within_root(posixpath.join(root, *crumbs[:i+1])))
    if len(crumbs) > 7:
        st.caption('Deeper path shown in directory bar above.')

    try:
        ssh, sftp = sftp_connection()
        try:
            checked_remote_path(sftp, current)
            attrs = sorted(sftp.listdir_attr(current), key=lambda x: (
                not stat.S_ISDIR(x.st_mode), x.filename.casefold()))
        finally:
            sftp.close(); ssh.close()
    except Exception as exc:
        show_error(exc)
        return

    folders = [a for a in attrs if stat.S_ISDIR(a.st_mode)]
    files = [a for a in attrs if not stat.S_ISDIR(a.st_mode)]
    search = st.text_input('🔎 Search this folder', placeholder='Filter folders and files by name')
    if search:
        attrs = [a for a in attrs if search.casefold() in a.filename.casefold()]
    st.caption(f'{len(folders)} folders · {len(files)} files · {len(attrs)} visible')

    left, right = st.columns([3.3, 1.5], gap='large')
    with left:
        if not attrs:
            st.info('This directory is empty, or no files match your search.')
        elif st.session_state.get('explorer_list', False):
            for idx, entry in enumerate(attrs):
                one, two, three = st.columns([5, 2, 2])
                with one:
                    _file_tile(entry, current, idx)
                with two:
                    st.caption(datetime.fromtimestamp(entry.st_mtime).strftime('%b %d, %Y'))
                with three:
                    st.caption('Directory' if stat.S_ISDIR(entry.st_mode) else _human_size(entry.st_size))
                st.divider()
        else:
            for start in range(0, len(attrs), 4):
                columns = st.columns(4)
                for column, index in zip(columns, range(start, min(start+4, len(attrs)))):
                    with column:
                        with st.container(border=True):
                            _file_tile(attrs[index], current, index)

    with right:
        st.subheader('Actions')
        with st.expander('📁 New folder', expanded=False):
            with st.form(f'mkdir_{current}', clear_on_submit=True):
                folder = st.text_input('Folder name')
                if st.form_submit_button('Create folder', use_container_width=True):
                    try:
                        path = safe_child(current, folder)
                        ssh, sftp = sftp_connection()
                        try:
                            checked_new_path(sftp, path)
                            sftp.mkdir(path)
                        finally:
                            sftp.close(); ssh.close()
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)

        with st.expander('⬆️ Upload files', expanded=False):
            uploads = st.file_uploader('Choose files', accept_multiple_files=True,
                                       key=f'upload_{current}')
            if st.button('Upload selected', use_container_width=True, disabled=not uploads):
                try:
                    ssh, sftp = sftp_connection()
                    try:
                        checked_remote_path(sftp, current)
                        for upload in uploads:
                            if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
                                raise ValueError(f'{upload.name} is larger than {MAX_UPLOAD_MB} MB.')
                            path = safe_child(current, upload.name)
                            checked_new_path(sftp, path)
                            # Avoid overwriting preexisting files silently.
                            with sftp.open(path, 'xb') as remote:
                                upload.seek(0)
                                while chunk := upload.read(1024 * 1024):
                                    remote.write(chunk)
                    finally:
                        sftp.close(); ssh.close()
                    st.success('Upload complete.')
                    st.rerun()
                except Exception as exc:
                    show_error(exc)

        selected = st.session_state.get('selected_item')
        item = next((a for a in attrs if a.filename == selected), None)
        if item is None or stat.S_ISDIR(item.st_mode):
            st.info('Choose a file tile to preview, download, or rename it. Select a folder tile to open it.')
            return
        path = safe_child(current, selected)
        st.markdown(f'**Selected:** `{selected}`')
        st.caption(f'Size: {_human_size(item.st_size)} · Modified: '
                   + datetime.fromtimestamp(item.st_mtime).strftime('%Y-%m-%d %H:%M'))
        # Links are not offered for download or editing to avoid symlink escapes.
        if stat.S_ISLNK(item.st_mode):
            st.warning('Symbolic link selected. Link editing is not available.')
            return
        if st.button('👁 Preview', use_container_width=True):
            st.session_state.preview_path = path
        if st.session_state.get('preview_path') == path:
            try:
                editor_preview(path)
            except Exception as exc:
                show_error(exc)
        if item.st_size <= MAX_DOWNLOAD_MB * 1024 * 1024:
            try:
                ssh, sftp = sftp_connection()
                try:
                    checked_remote_path(sftp, path)
                    with sftp.open(path, 'rb') as f:
                        contents = f.read(MAX_DOWNLOAD_MB * 1024 * 1024 + 1)
                finally:
                    sftp.close(); ssh.close()
                st.download_button('⬇ Download', contents, file_name=selected,
                                   use_container_width=True)
            except Exception as exc:
                show_error(exc)
        else:
            st.warning(f'Download exceeds {MAX_DOWNLOAD_MB} MB browser limit.')

        with st.expander('✏️ Rename'):
            new_name = st.text_input('New file name', value=selected, key=f'rename_{current}_{selected}')
            if st.button('Rename file', key=f'do_rename_{current}_{selected}'):
                try:
                    new_path = safe_child(current, new_name)
                    if new_path == path:
                        st.info('File name is unchanged.')
                    else:
                        ssh, sftp = sftp_connection()
                        try:
                            checked_remote_path(sftp, path)
                            checked_new_path(sftp, new_path)
                            try:
                                sftp.lstat(new_path)
                                raise FileExistsError('Destination already exists.')
                            except FileNotFoundError:
                                pass
                            sftp.rename(path, new_path)
                        finally:
                            sftp.close(); ssh.close()
                        st.session_state.selected_item = new_name
                        st.rerun()
                except Exception as exc:
                    show_error(exc)

        with st.expander('🗑 Delete file'):
            st.warning('Deleting a file from PSC cannot be undone here.')
            confirmed = st.checkbox(f'I want to permanently delete {selected}', key=f'confirm_{current}_{selected}')
            if st.button('Delete selected file', disabled=not confirmed, type='secondary'):
                try:
                    ssh, sftp = sftp_connection()
                    try:
                        checked_remote_path(sftp, path)
                        sftp.remove(path)
                    finally:
                        sftp.close(); ssh.close()
                    st.session_state.selected_item = None
                    st.rerun()
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
