"""Interactive 3Dmol.js molecular and Gaussian cube visualization.

Uses client-side Javascript, no uploaded molecular data is sent to 3rd-party API.
"""
import base64
import json
from pathlib import PurePosixPath
import streamlit as st
import streamlit.components.v1 as components

MOLECULE_EXTENSIONS = {'.pdb', '.sdf', '.cif', '.mol2', '.xyz', '.cube', '.cub'}
FORMAT_MAP = {'.pdb': 'pdb', '.sdf': 'sdf', '.cif': 'cif', '.mol2': 'mol2', '.xyz': 'xyz', '.cube': 'cube', '.cub': 'cube'}
CUBE_EXTENSIONS = {'.cube', '.cub'}


def render_molecule_viewer(structure_text: str, filename: str,
                           cube_text: str | None = None, cube_filename: str | None = None) -> None:
    """Draw atoms, measure geometry on clicks, and show signed cube isosurfaces."""
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in FORMAT_MAP:
        st.warning('Unsupported molecular file format.'); return
    is_cube = ext in CUBE_EXTENSIONS
    if is_cube and cube_text is None:
        cube_text = structure_text
    has_cube = cube_text is not None
    st.markdown('#### 🧬 3D molecular viewer')
    first = st.columns([2, 1.3, 1.2], vertical_alignment='bottom')
    with first[0]:
        display = st.selectbox('Molecule appearance', ['Ball and stick', 'Sticks', 'Space filling', 'Lines', 'Cartoon (proteins)'], key='mol_style_'+filename)
    with first[1]:
        background = st.selectbox('Background', ['White', 'Black', 'Light blue'], key='mol_bg_'+filename)
    with first[2]:
        spin = st.toggle('Auto rotate', value=False, key='mol_spin_'+filename)
    show_cell = st.checkbox('Show unit cell (if available)', value=False, key='mol_cell_'+filename) if ext == '.cif' else False

    mode = 'Orbitals (positive and negative phases)'
    isovalue = 0.03
    opacity = 0.65
    if has_cube:
        with st.expander('Electron density / orbital surfaces', expanded=True):
            a,b,c = st.columns([2,1,1])
            with a:
                mode = st.selectbox('Cube interpretation', ['Orbitals (positive and negative phases)', 'Electron density (positive values)', 'Signed field (negative values)', 'Hide isosurface'], key='cube_mode_'+filename)
            with b:
                isovalue = st.number_input('Absolute isovalue', min_value=0.0001, max_value=20.0, value=0.03, step=0.01, format='%.4f', key='cube_iso_'+filename)
            with c:
                opacity = st.slider('Opacity', 0.1, 1.0, 0.65, step=0.05, key='cube_op_'+filename)
            st.caption('Blue/red show the sign of a scalar field, not electrostatic charge. For an electron-density cube, select the positive-only surface. Units and meaning depend on the generating program.')

    payload = {
        'structure': base64.b64encode(structure_text.encode('utf-8')).decode('ascii'),
        'format': FORMAT_MAP[ext],
        'cube': base64.b64encode(cube_text.encode('utf-8')).decode('ascii') if has_cube else None,
        'style': display, 'bg': background, 'spin': spin, 'cell': show_cell,
        'surfaceMode': mode, 'isovalue': isovalue, 'opacity': opacity,
    }
    settings = json.dumps(payload, ensure_ascii=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html = r'''<!doctype html><html><head><meta charset="utf-8"/>
<script src="https://cdn.jsdelivr.net/npm/3dmol@2.5.5/build/3Dmol-min.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;font:13px 'Segoe UI',Arial,sans-serif;color:#242a32;background:#fff}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 11px;background:#f7f9fc;border-bottom:1px solid #dfe3e9}
.toolbar label{white-space:nowrap}.toolbar select,.toolbar button{border:1px solid #bbc6d4;background:white;border-radius:5px;padding:5px 8px;font:inherit}
.toolbar button{cursor:pointer}.toolbar button:hover{background:#e5efff}#viewer{height:535px;width:100%;position:relative;background:#fff}
#status{padding:9px 12px;border-top:1px solid #dfe3e9;background:#fafbfd;min-height:36px}
#history{padding:0 12px 8px;color:#536172;background:#fafbfd;min-height:22px}
</style></head><body>
<div class="toolbar"><label>Measure <select id="measure"><option value="off">Off</option><option value="distance">Distance (2 atoms)</option><option value="angle">Angle (3 atoms)</option><option value="dihedral">Dihedral (4 atoms)</option></select></label>
<label><input id="numbers" type="checkbox"/> Atom numbers</label>
<label><input id="elements" type="checkbox"/> Element labels</label>
<button id="reset">Clear measurements</button><button id="fit">Fit structure</button></div>
<div id="viewer"></div><div id="status">Drag to rotate · Scroll to zoom · Right-drag to pan. Choose a measurement and click atoms in sequence.</div><div id="history"></div>
<script>
'use strict';
const cfg=__CONFIG__;
const $=id=>document.getElementById(id);
function decode(b64){const bin=atob(b64),arr=Uint8Array.from(bin,x=>x.charCodeAt(0));return new TextDecoder().decode(arr)}
function v(a){return [a.x,a.y,a.z]}
function sub(a,b){return a.map((x,i)=>x-b[i])}
function dot(a,b){return a.reduce((s,x,i)=>s+x*b[i],0)}
function cross(a,b){return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]}
function norm(a){return Math.sqrt(dot(a,a))}
function unit(a){const n=norm(a);return n>1e-12?a.map(x=>x/n):null}
function distance(a,b){return norm(sub(v(a),v(b)))}
function angle(a,b,c){const x=unit(sub(v(a),v(b))),y=unit(sub(v(c),v(b)));return x&&y?Math.acos(Math.max(-1,Math.min(1,dot(x,y))))*180/Math.PI:NaN}
function dihedral(a,b,c,d){const p=v(a),q=v(b),r=v(c),s=v(d),b0=sub(p,q),b1=sub(r,q),b2=sub(s,r),u=unit(b1);if(!u)return NaN;const x=sub(b0,u.map(z=>dot(b0,u)*z)),y=sub(b2,u.map(z=>dot(b2,u)*z));if(norm(x)<1e-10||norm(y)<1e-10)return NaN;return Math.atan2(dot(cross(u,x),y),dot(x,y))*180/Math.PI}
try {
 if(!window.$3Dmol)throw Error('3Dmol.js could not load. Check access to jsDelivr.');
 const viewer=$3Dmol.createViewer($('viewer'),{backgroundColor:cfg.bg==='Black'?'#101827':cfg.bg==='Light blue'?'#edf5ff':'white'});
 const text=decode(cfg.structure);
 const model=viewer.addModel(text,cfg.format);
 const atoms=model.selectedAtoms({});
 if(!atoms.length)throw Error('No atoms could be parsed from the structure.');
 if(cfg.style==='Ball and stick')viewer.setStyle({},{stick:{radius:.15},sphere:{scale:.28}});
 else if(cfg.style==='Sticks')viewer.setStyle({},{stick:{radius:.18}});
 else if(cfg.style==='Space filling')viewer.setStyle({},{sphere:{scale:1}});
 else if(cfg.style==='Lines')viewer.setStyle({},{line:{linewidth:1.5}});
 else viewer.setStyle({},{cartoon:{color:'spectrum'},stick:{radius:.12}});
 if(cfg.cell)try{viewer.addUnitCell(model,{box:{color:'#536579'}})}catch(e){console.warn(e)}
 if(cfg.cube&&cfg.surfaceMode!=='Hide isosurface'){
   const cube=decode(cfg.cube);
   const vol=new $3Dmol.VolumeData(cube,'cube');
   const spec=(value,color)=>viewer.addIsosurface(vol,{isoval:value,color:color,opacity:cfg.opacity,smoothness:2});
   if(cfg.surfaceMode==='Orbitals (positive and negative phases)'){spec(cfg.isovalue,'#2872d0');spec(-cfg.isovalue,'#dd584f')}
   else if(cfg.surfaceMode==='Electron density (positive values)')spec(cfg.isovalue,'#247cc1');
   else spec(-cfg.isovalue,'#d65753');
 }
 const atIndex=new Map(atoms.map((a,i)=>[a,i+1]));
 let picks=[], marks=[], lines=[], measurementHistory=[];
 let labels=[];
 function updateLabels(){labels.forEach(x=>viewer.removeLabel(x));labels=[];
  const num=$('numbers').checked, elem=$('elements').checked;
  if(num||elem){
    const maxLabels=1500;
    atoms.slice(0,maxLabels).forEach((a,i)=>{const s=(elem?(a.elem||'X'):'')+(num?(elem?' ':'')+(i+1):'');
      labels.push(viewer.addLabel(s,{position:a,fontSize:11,fontColor:'#101a30',backgroundColor:'#ffffff',backgroundOpacity:.55,borderThickness:0,inFront:true}));
    });
    if(atoms.length>maxLabels)$('status').textContent='Labels limited to the first 1,500 atoms for browser responsiveness.';
  }
  viewer.render();
 }
 $('numbers').addEventListener('change',updateLabels);$('elements').addEventListener('change',updateLabels);
 function clear(){picks=[];marks.forEach(x=>viewer.removeLabel(x));marks=[];lines.forEach(x=>viewer.removeShape(x));lines=[];measurementHistory=[];$('history').textContent='';viewer.render();$('status').textContent='Choose a measurement and click atoms in sequence.'}
 $('reset').addEventListener('click',clear);$('fit').addEventListener('click',()=>{viewer.zoomTo();viewer.render()});
 $('measure').addEventListener('change',()=>{picks=[];$('status').textContent='Click '+({distance:'2',angle:'3',dihedral:'4'}[$('measure').value]||'0')+' atoms to measure.'});
 function pickAtom(a){const mode=$('measure').value;if(mode==='off')return;
 const required={distance:2,angle:3,dihedral:4}[mode];
 if(picks.includes(a)){$('status').textContent='Choose distinct atoms.';return}
 picks.push(a);marks.push(viewer.addLabel(String(atIndex.get(a)||a.index+1),{position:a,fontSize:14,backgroundColor:'#facc15',backgroundOpacity:.92,fontColor:'#111827',inFront:true}));
 $('status').textContent='Selected: '+picks.map(x=>(x.elem||'X')+(atIndex.get(x)||'?')).join(' → ')+` (${picks.length}/${required})`;
 if(picks.length===required){let value,units;if(mode==='distance'){value=distance(...picks);units='Å'}else if(mode==='angle'){value=angle(...picks);units='°'}else{value=dihedral(...picks);units='°'}
  for(let i=1;i<picks.length;i++){lines.push(viewer.addLine({start:v(picks[i-1]),end:v(picks[i]),color:'#f59e0b',linewidth:3,dashed:true}))}
  const summary=mode+' '+picks.map(x=>(x.elem||'X')+(atIndex.get(x)||'?')).join('-')+': '+(Number.isFinite(value)?value.toFixed(3)+units:'undefined geometry');
  measurementHistory.push(summary);$('history').textContent=measurementHistory.slice(-4).join('   |   ');$('status').textContent=summary+' · Select more atoms for the next measurement.';
  picks=[];
 }
 viewer.render();}
 viewer.setClickable({},true,(atom)=>pickAtom(atom));
 viewer.zoomTo();viewer.render();if(cfg.spin)viewer.spin('y',.5);
} catch(e){$('status').textContent='Unable to render: '+e.message;console.error(e)}
</script></body></html>'''.replace('__CONFIG__',settings)
    components.html(html,height=650,scrolling=False)
    st.caption('Measurements use displayed Cartesian coordinates (Å and degrees). CIF symmetry-related bonds across cell boundaries may require an expanded structure. Cube surfaces are rendered locally in the browser using 3Dmol.js.')
