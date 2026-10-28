"""Safe, client-side 3D structure viewing for the Bridges-2 student file explorer.

Molecular text is passed as base64 to a 3Dmol.js iframe, never interpolated
as raw HTML/JS. 3Dmol is downloaded from a public CDN by the student's browser.
"""
import base64
import json
from pathlib import PurePosixPath

import streamlit as st
import streamlit.components.v1 as components

MOLECULE_EXTENSIONS = {'.pdb', '.sdf', '.cif', '.mol2', '.xyz'}
FORMAT_MAP = {'.pdb': 'pdb', '.sdf': 'sdf', '.cif': 'cif', '.mol2': 'mol2', '.xyz': 'xyz'}


def render_molecule_viewer(structure_text: str, filename: str) -> None:
    """Interactive browser viewer with bonds, atoms, proteins, and crystals."""
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in FORMAT_MAP:
        st.warning('Unsupported structure format.')
        return

    with st.container(border=True):
        st.markdown('#### 🧬 Molecular viewer')
        ctrl = st.columns([2.3, 1.3, 1.2], vertical_alignment='bottom')
        with ctrl[0]:
            display = st.selectbox('Display', ['Ball and stick', 'Sticks', 'Space filling', 'Lines', 'Cartoon (proteins)'],
                                   key='mol_style_' + filename)
        with ctrl[1]:
            background = st.selectbox('Background', ['White', 'Black', 'Light blue'], key='mol_bg_' + filename)
        with ctrl[2]:
            spin = st.toggle('Auto rotate', value=False, key='mol_spin_' + filename)
        show_cell = False
        if ext == '.cif':
            show_cell = st.checkbox('Show unit cell (when available)', value=True, key='mol_cell_' + filename)
            st.caption('CIF structures may represent a crystal asymmetric unit. Displayed bonding and symmetry depend on the CIF contents and parser.')
        elif ext == '.xyz':
            st.caption('XYZ files contain atom positions; bonds are inferred by the viewer.')

        encoded = base64.b64encode(structure_text.encode('utf-8')).decode('ascii')
        settings = json.dumps({'data': encoded, 'format': FORMAT_MAP[ext], 'style': display,
                               'background': background, 'spin': spin, 'cell': show_cell})
        # settings contains only base64 and constrained values. Escape HTML script terminators as defense in depth.
        settings = settings.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        html = '''<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#f8fafc;font-family:Segoe UI,Arial,sans-serif}
#viewer{height:510px;width:100%;position:relative;border-radius:8px;overflow:hidden}
#notice{font-size:13px;color:#475569;padding:6px 12px}</style>
<script src="https://cdn.jsdelivr.net/npm/3dmol@2.5.5/build/3Dmol-min.js"></script>
</head><body><div id="viewer"></div><div id="notice">Drag to rotate · Scroll to zoom · Right-drag to pan</div>
<script>
const config=__CONFIG__;
function decodeUTF8(encoded){const b=atob(encoded);const v=Uint8Array.from(b,c=>c.charCodeAt(0));return new TextDecoder('utf-8').decode(v);}
try {
 if(!window.$3Dmol) throw new Error('3D viewer library did not load. Check browser access to jsDelivr.');
 const viewer=$3Dmol.createViewer(document.getElementById('viewer'),{backgroundColor:config.background==='Black'?'#101827':config.background==='Light blue'?'#edf5ff':'white'});
 const model=viewer.addModel(decodeUTF8(config.data),config.format);
 const style=config.style;
 if(style==='Ball and stick') viewer.setStyle({}, {stick:{radius:0.15},sphere:{scale:0.28}});
 else if(style==='Sticks') viewer.setStyle({}, {stick:{radius:0.18}});
 else if(style==='Space filling') viewer.setStyle({}, {sphere:{scale:1.0}});
 else if(style==='Lines') viewer.setStyle({}, {line:{linewidth:1.5}});
 else viewer.setStyle({}, {cartoon:{color:'spectrum'},stick:{radius:0.12}});
 if(config.cell && typeof viewer.addUnitCell==='function') {
   try { viewer.addUnitCell(model,{box:{color:'#5b6472'}}); } catch(e) { console.warn('Unit cell unavailable:', e); }
 }
 viewer.zoomTo();viewer.render();
 if(config.spin)viewer.spin('y',0.5);
} catch(err){document.getElementById('notice').textContent='Unable to render structure: '+err.message;}
</script></body></html>'''.replace('__CONFIG__', settings)
        components.html(html, height=552, scrolling=False)
        st.caption('3Dmol.js viewer · Drag to rotate, scroll to zoom, right-drag to pan. Structure remains in the current browser session; the viewer does not upload it to a molecular database.')
