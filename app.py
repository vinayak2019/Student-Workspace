import io
import os
import posixpath
import re
import shlex
import stat
import secrets
import threading
import time
import hashlib
from datetime import timedelta
from datetime import datetime

import paramiko
import streamlit as st
from streamlit_cookies_controller import CookieController

from molecule_viewer import MOLECULE_EXTENSIONS, render_molecule_viewer
from molecule_builder import molecule_builder_page

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

# An opt-in, short-lived browser token restores a session after a hard refresh.
# PSC credentials are NEVER stored in browser cookies; only in server process
# memory. A Streamlit Cloud process restart invalidates all remembered logins.
REMEMBER_SECONDS = 4 * 60 * 60
REMEMBER_COOKIE = 'b2_workspace_session'

@st.cache_resource
def credential_vault():
    return {'lock': threading.RLock(), 'entries': {}}


def cookie_controller():
    # Do not cache globally: the component must remain scoped to this browser.
    if '_b2_cookie_controller' not in st.session_state:
        st.session_state['_b2_cookie_controller'] = CookieController(key='b2_cookie_controller')
    return st.session_state['_b2_cookie_controller']


def _clear_expired(entries):
    now = time.time()
    for key, item in list(entries.items()):
        if item['expires'] <= now:
            del entries[key]


def remember_login(username, password, root):
    token = secrets.token_urlsafe(32)
    key = hashlib.sha256(token.encode('ascii')).hexdigest()
    vault = credential_vault()
    with vault['lock']:
        _clear_expired(vault['entries'])
        vault['entries'][key] = dict(username=username, password=password,
                                     root=root, expires=time.time() + REMEMBER_SECONDS)
    cookie_controller().set(REMEMBER_COOKIE, token, max_age=REMEMBER_SECONDS,
                            expires=datetime.now() + timedelta(seconds=REMEMBER_SECONDS),
                            secure=True, same_site='strict', path='/')
    st.session_state['_b2_remember_token'] = token
    # Do not rerun immediately: component commands must reach the browser first.


def _browser_remember_token():
    """Prefer request cookies, available immediately after a full refresh.

    The cookie component may not be initialized on the first Streamlit run,
    so calling its get() alone can wrongly show the login screen.
    """
    try:
        token = st.context.cookies.get(REMEMBER_COOKIE)
        if isinstance(token, str) and token:
            return token
    except (AttributeError, KeyError, TypeError):
        pass
    try:
        return cookie_controller().get(REMEMBER_COOKIE)
    except Exception:
        return None


def restore_login():
    token = _browser_remember_token()
    if not isinstance(token, str) or len(token) < 32:
        return False
    key = hashlib.sha256(token.encode('ascii')).hexdigest()
    vault = credential_vault()
    with vault['lock']:
        _clear_expired(vault['entries'])
        entry = vault['entries'].get(key)
        if entry is None:
            return False
        credentials = entry.copy()
    st.session_state.username = credentials['username']
    st.session_state.password = credentials['password']
    st.session_state.root = credentials['root']
    st.session_state.current = credentials['root']
    st.session_state.folder_history = []
    st.session_state.view = 'File Explorer'
    st.session_state['_b2_remember_token'] = token
    return True


def logout_user():
    token = st.session_state.get('_b2_remember_token')
    if not token:
        token = _browser_remember_token()
    if isinstance(token, str):
        key = hashlib.sha256(token.encode('ascii')).hexdigest()
        vault = credential_vault()
        with vault['lock']:
            vault['entries'].pop(key, None)
    try:
        cookie_controller().remove(REMEMBER_COOKIE)
    except Exception:
        pass
    st.session_state.clear()
    st.rerun()



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


st.markdown("""<style>
/* Windows Explorer-inspired chrome, without boxed file icons. */
[data-testid="stSidebar"], [data-testid="collapsedControl"] {display:none!important;}
.block-container {padding-top:.85rem;max-width:1450px;padding-left:2rem;padding-right:2rem;}
[data-testid="stAppViewContainer"] {background:#f8fafc;}
[data-testid="stHeader"] {background:transparent;}
h1 {font-size:1.65rem!important;font-weight:650!important;letter-spacing:-.025em;}
h2,h3 {font-size:1.12rem!important;}
.stButton button {border-radius:5px;min-height:2.3rem;}
/* Explorer tiles: the icon is a real scalable pseudo-element, not button text. */
[class*="st-key-tile_"] button {background:transparent!important;border:1px solid transparent!important;
 box-shadow:none!important;color:#1f2937!important;min-height:125px!important;
 display:flex!important;flex-direction:column!important;align-items:center!important;
 justify-content:center!important;gap:.22rem!important;
 white-space:normal!important;text-align:center!important;font-weight:450!important;
 padding:.5rem .25rem!important;}
[class*="st-key-tile_"] button::before {
 content:"📄";display:block;font-family:"Segoe UI Emoji","Apple Color Emoji",sans-serif;
 font-size:var(--explorer-icon-size, 5rem)!important;
 line-height:1.08!important;flex-shrink:0!important;
}
/* Specificity matches the actual Streamlit container key: tile_<kind>_<index>. */
[class*="st-key-tile_folder_"] button::before {content:"📁";}
[class*="st-key-tile_link_"] button::before {content:"🔗";}
[class*="st-key-tile_job_"] button::before {content:"⚙️";}
[class*="st-key-tile_log_"] button::before {content:"📜";}
[class*="st-key-tile_python_"] button::before {content:"🐍";}
[class*="st-key-tile_input_"] button::before {content:"🧪";}
[class*="st-key-tile_data_"] button::before {content:"📊";}
[class*="st-key-tile_image_"] button::before {content:"🖼️";}
[class*="st-key-tile_archive_"] button::before {content:"🗜️";}
[class*="st-key-tile_pdf_"] button::before {content:"📕";}
[class*="st-key-tile_text_"] button::before {content:"📝";}
[class*="st-key-tile_"] button:hover {background:#e8f2fc!important;border-color:#c8e3fb!important;}
[class*="st-key-tile_"] button[kind="primary"] {background:#dceefe!important;border-color:#a9d5fa!important;}
[class*="st-key-tile_"] button p {overflow-wrap:anywhere;white-space:normal!important;
 line-height:1.25!important;font-size:.83rem!important;margin:0!important;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:6px;}
[data-testid="stPopover"] button {border-radius:5px;}
[data-testid="stCaptionContainer"] {font-size:.76rem;}
</style>""", unsafe_allow_html=True)


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
    """Open a folder; remember the previous location for Back."""
    path = ensure_within_root(path)
    previous = st.session_state.current
    if path != previous:
        history = st.session_state.setdefault('folder_history', [])
        history.append(previous)
    st.session_state.current = path
    st.session_state.selected_item = None
    st.rerun()


def _back():
    """Return from a tool screen to Files, or navigate folder history."""
    if st.session_state.get('view', 'File Explorer') != 'File Explorer':
        st.session_state.view = 'File Explorer'
    else:
        history = st.session_state.setdefault('folder_history', [])
        if history:
            st.session_state.current = history.pop()
        elif st.session_state.current != st.session_state.root:
            st.session_state.current = ensure_within_root(posixpath.dirname(st.session_state.current))
        else:
            return
        st.session_state.selected_item = None
    st.rerun()


def _open_view(page):
    st.session_state.view = page
    st.rerun()


def _actions_menu(key_prefix):
    """Shared menu, available from every screen."""
    with st.popover('⚡ Actions', use_container_width=True):
        for label, page in [('📁 Files', 'File Explorer'),
                            ('🧪 Molecule builder', 'Molecule Builder'),
                            ('🚀 Submit simulation', 'Submit Simulation'),
                            ('📊 Job monitor', 'Job Monitor'),
                            ('📄 Job logs', 'Job Logs'),
                            ('⚙️ Account', 'Account')]:
            if st.button(label, key=f'{key_prefix}_{page}', use_container_width=True):
                _open_view(page)
        if st.button('🚪 Log out', key=f'{key_prefix}_logout', use_container_width=True):
            logout_user()


def _file_tile(entry, current, index):
    name = entry.filename
    directory = stat.S_ISDIR(entry.st_mode)
    symlink = stat.S_ISLNK(entry.st_mode)
    selected = st.session_state.get('selected_item') == name
    extension = posixpath.splitext(name.lower())[1]
    if directory:
        kind = 'folder'
    elif symlink:
        kind = 'link'
    elif extension in ('.sbatch', '.slurm', '.job'):
        kind = 'job'
    elif extension in ('.out', '.err', '.log', '.stderr', '.stdout'):
        kind = 'log'
    elif extension in ('.py', '.ipynb'):
        kind = 'python'
    elif extension in ('.in', '.inp', '.xyz', '.cif', '.pdb', '.sdf', '.mol', '.mol2'):
        kind = 'input'
    elif extension in ('.csv', '.tsv', '.json', '.xml', '.dat', '.h5', '.hdf5', '.xlsx'):
        kind = 'data'
    elif extension in ('.png', '.jpg', '.jpeg', '.svg', '.gif', '.webp'):
        kind = 'image'
    elif extension in ('.zip', '.tar', '.gz', '.bz2', '.7z'):
        kind = 'archive'
    elif extension == '.pdf':
        kind = 'pdf'
    elif extension in ('.txt', '.md', '.rst'):
        kind = 'text'
    else:
        kind = 'file'
    # CSS draws the icon separately from the label, so icon size scales correctly.
    with st.container(key=f'tile_{kind}_{index}'):
        if st.button(name, key=f'file_button_{current}_{index}',
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


def file_manager():
    current = st.session_state.current
    root = st.session_state.root
    rel = posixpath.relpath(current, root)
    crumbs = [] if rel == '.' else rel.split('/')

    header_left, header_right = st.columns([5, 2], vertical_alignment='center')
    with header_left:
        st.title('📁 Files')
    with header_right:
        st.caption('Bridges-2  •  ' + st.session_state.username)

    buttons = st.columns([.8, .8, .7, 1.3, 1.45, 1.55, 3.6], vertical_alignment='center')
    with buttons[0]:
        if st.button('← Back', disabled=(current == root and not st.session_state.get('folder_history')), use_container_width=True):
            _back()
    with buttons[1]:
        if st.button('⌂ Home', use_container_width=True):
            _navigate(root)
    with buttons[2]:
        if st.button('↻', use_container_width=True, help='Refresh'):
            st.rerun()
    with buttons[3]:
        st.toggle('List view', key='explorer_list')
    with buttons[4]:
        with st.popover('⚡ Actions', use_container_width=True):
            st.markdown('**Files & folders**')
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
                                with sftp.open(path, 'wx') as remote:
                                    upload.seek(0)
                                    while chunk := upload.read(1024 * 1024):
                                        remote.write(chunk)
                        finally:
                            sftp.close(); ssh.close()
                        st.success('Upload complete.')
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
            
            st.divider()
            st.markdown('**Workspace**')
            for label, page in [('🧪 Molecule builder', 'Molecule Builder'),
                                ('🚀 Submit simulation', 'Submit Simulation'),
                                ('📊 Job monitor', 'Job Monitor'),
                                ('📄 Job logs', 'Job Logs'),
                                ('⚙️ Account', 'Account')]:
                if st.button(label, key='action_' + page, use_container_width=True):
                    _open_view(page)
            if st.button('🚪 Log out', use_container_width=True):
                logout_user()
    with buttons[5]:
        st.selectbox('Icon size', ['Small', 'Medium', 'Large', 'Extra large'], index=2, key='explorer_icon_size', label_visibility='collapsed', help='Change the size of file and folder icons')
    with buttons[6]:
        st.text_input('Search', key='explorer_search', placeholder='🔎 Search files', label_visibility='collapsed')

    # Breadcrumbs are the familiar Explorer address bar.
    breadcrumb_cols = st.columns(min(8, len(crumbs) + 1))
    with breadcrumb_cols[0]:
        if st.button('This PC  ›  Project', key='crumb_root'):
            _navigate(root)
    for i, part in enumerate(crumbs[:7]):
        with breadcrumb_cols[min(i+1, len(breadcrumb_cols)-1)]:
            if st.button('› ' + part, key=f'crumb_{i}'):
                _navigate(ensure_within_root(posixpath.join(root, *crumbs[:i+1])))

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

    search = st.session_state.get('explorer_search', '')
    if search:
        attrs = [a for a in attrs if search.casefold() in a.filename.casefold()]
    nfolders = sum(stat.S_ISDIR(a.st_mode) for a in attrs)
    st.caption(f'{len(attrs)} items  •  {nfolders} folders')

    size_settings = {
        'Small': (9, 44, '2.5rem'),
        'Medium': (7, 60, '3.75rem'),
        'Large': (5, 88, '5.25rem'),
        'Extra large': (4, 120, '7rem'),
    }
    cols_per_row, tile_height, icon_font = size_settings[st.session_state.get('explorer_icon_size', 'Large')]
    st.markdown(f'''<style>
        :root {{ --explorer-icon-size: {icon_font}; }}
        [class*="st-key-tile_"] button {{min-height:{tile_height + 45}px!important;}}
    </style>''', unsafe_allow_html=True)

    if not attrs:
        st.info('No items to display.')
    elif st.session_state.get('explorer_list', False):
        header = st.columns([7, 3])
        header[0].caption('Name')
        header[1].caption('Date modified')
        st.divider()
        for idx, entry in enumerate(attrs):
            one, two = st.columns([7, 3], vertical_alignment='center')
            with one:
                _file_tile(entry, current, idx)
            with two:
                st.caption(datetime.fromtimestamp(entry.st_mtime).strftime('%b %d, %Y'))
    else:
        for start in range(0, len(attrs), cols_per_row):
            columns = st.columns(cols_per_row)
            for column, index in zip(columns, range(start, min(start+cols_per_row, len(attrs)))):
                with column:
                    _file_tile(attrs[index], current, index)

    selected = st.session_state.get('selected_item')
    item = next((a for a in attrs if a.filename == selected), None)
    if item is not None and not stat.S_ISDIR(item.st_mode):
        st.divider()
        st.markdown('**Selected file**')
        file_actions(item, current)


def molecule_preview(path, filename):
    """Fetch a bounded molecule file through the existing authenticated SFTP channel."""
    ssh, sftp = sftp_connection()
    try:
        checked_remote_path(sftp, path)
        attributes = sftp.stat(path)
        max_bytes = 8 * 1024 * 1024
        if attributes.st_size > max_bytes:
            st.warning('This structure exceeds the 8 MB browser viewer limit. Download it to view locally.')
            return
        with sftp.open(path, 'rb') as remote:
            raw = remote.read(max_bytes + 1)
        if len(raw) > max_bytes:
            st.warning('Structure exceeds the 8 MB browser viewer limit.')
            return
        if b'\x00' in raw:
            st.error('The structure does not look like a plain-text molecular file.')
            return
        render_molecule_viewer(raw.decode('utf-8', errors='replace'), filename)
    finally:
        sftp.close()
        ssh.close()


def file_actions(item, current):
    selected = item.filename
    path = safe_child(current, selected)
    st.markdown(f'**Selected:** `{selected}`')
    st.caption('Modified: ' + datetime.fromtimestamp(item.st_mtime).strftime('%Y-%m-%d %H:%M'))
    # Links are not offered for download or editing to avoid symlink escapes.
    if stat.S_ISLNK(item.st_mode):
        st.warning('Symbolic link selected. Link editing is not available.')
        return
    is_molecule = posixpath.splitext(selected)[1].lower() in MOLECULE_EXTENSIONS
    if is_molecule:
        if st.button('🧬 Open 3D molecule viewer', use_container_width=True, type='primary'):
            st.session_state.molecule_path = path
        if st.session_state.get('molecule_path') == path:
            try:
                molecule_preview(path, selected)
            except Exception as exc:
                show_error(exc)
    if st.button('👁 Preview as text', use_container_width=True):
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
    st.caption('Browse output and error files from Files.')
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
    st.write('Sign in using your PSC account. Optional refresh persistence uses a short-lived browser token, not a browser-stored PSC password.')
    if 'PROJECT_ID' in ROOT_TEMPLATE:
        st.warning('Administrator: configure B2_WORKSPACE_ROOT before enabling student access.')
        st.stop()
    with st.form('login'):
        username = st.text_input('PSC username')
        password = st.text_input('PSC password', type='password')
        keep_signed_in = st.checkbox('Keep me signed in after refreshing (up to 4 hours)', value=False)
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
            st.session_state.folder_history = []
            st.session_state.view = 'File Explorer'
            if keep_signed_in:
                remember_login(username, password, root)
            if keep_signed_in:
                st.success('Connected. Select **Open workspace** below to finish saving this browser sign-in.')
            else:
                st.success('Connected. Select **Open workspace** to continue.')
        except Exception as exc:
            show_error(exc)
    if 'username' in st.session_state:
        if st.button('Open workspace', type='primary', key='finish_login'):
            st.rerun()
    st.caption('PSC may require additional authentication or a supported access method. If password authentication is unavailable, use PSC Open OnDemand while an approved SSH authentication integration is configured.')


if 'username' not in st.session_state:
    if not restore_login():
        if _browser_remember_token():
            st.info('Your saved sign-in cannot be restored. The four-hour period may have expired, or the Streamlit Cloud app may have restarted. Please sign in again.')
        login_screen()
        st.stop()

# The same Back and Actions navigation is available on all tool screens.
view = st.session_state.get('view', 'File Explorer')
if view != 'File Explorer':
    nav = st.columns([1.4, 1.6, 7], vertical_alignment='center')
    with nav[0]:
        if st.button('← Back', key='tool_back', use_container_width=True):
            _back()
    with nav[1]:
        _actions_menu('tool_action')

if view == 'File Explorer':
    file_manager()
elif view == 'Molecule Builder':
    molecule_builder_page(sftp_connection, checked_new_path, checked_remote_path, safe_child, show_error)
elif view == 'Submit Simulation':
    submit_job()
elif view == 'Job Monitor':
    jobs_page()
elif view == 'Job Logs':
    logs_page()
else:
    st.subheader('⚙️ Account')
    st.write('PSC username:', st.session_state.username)
    st.write('Login host:', LOGIN_HOST)
    st.write('Data transfer host:', DATA_HOST)
    st.code(st.session_state.root)
    if st.button('View my Unix groups'):
        try:
            st.code(ssh_command(['id', '-Gn']))
        except Exception as exc:
            show_error(exc)
