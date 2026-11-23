#!/usr/bin/env python3
"""Student molecular DFT workflows: optimize/orbitals, frequencies, rigid scans.

Run ONLY under a compute-node allocation (e.g. sbatch run_simulation.sh).
Coordinates are in angstroms; atom indices in JSON are 1-based.
This program treats isolated molecules, not periodic CIF crystals.
"""
import argparse
import csv
import json
import logging
import os
from pathlib import Path
import sys

import numpy as np

HARTREE_TO_KJMOL = 2625.4996394799


def load_xyz(path):
    lines = Path(path).read_text().splitlines()
    if not lines:
        raise ValueError('Empty XYZ file')
    n = int(lines[0].strip())
    if n < 1 or len(lines) < n + 2:
        raise ValueError('XYZ file must contain a valid atom count, comment, and N atom lines')
    symbols, coords = [], []
    for line in lines[2:2+n]:
        words = line.split()
        if len(words) < 4:
            raise ValueError(f'Bad atom line: {line}')
        symbols.append(words[0])
        coords.append([float(x) for x in words[1:4]])
    return symbols, np.asarray(coords, dtype=float)


def write_xyz(path, symbols, coords, comment=''):
    with open(path, 'w') as f:
        f.write(f'{len(symbols)}\n{comment}\n')
        for s, xyz in zip(symbols, coords):
            f.write(f'{s:<3s} {xyz[0]: .10f} {xyz[1]: .10f} {xyz[2]: .10f}\n')


def as_pyscf_molecule(symbols, coords, cfg):
    from pyscf import gto
    return gto.M(
        atom=[(s, tuple(p)) for s, p in zip(symbols, coords)],
        unit='Angstrom',
        basis=cfg.get('basis', 'def2-svp'),
        charge=int(cfg.get('charge', 0)),
        spin=int(cfg.get('spin', 0)),
        verbose=3,
        max_memory=int(cfg.get('memory_mb', 3500)),
    )


def run_scf(mol, cfg):
    from pyscf import dft, scf
    method = cfg.get('method', 'PBE0').lower()
    if method == 'hf':
        mf = scf.RHF(mol) if mol.spin == 0 else scf.UHF(mol)
    else:
        mf = dft.RKS(mol) if mol.spin == 0 else dft.UKS(mol)
        mf.xc = cfg.get('method', 'PBE0')
        mf.grids.level = int(cfg.get('grid_level', 3))
    mf.conv_tol = float(cfg.get('scf_tolerance', 1e-8))
    mf.max_cycle = int(cfg.get('max_scf_cycles', 100))
    energy = mf.kernel()
    if not mf.converged or not np.isfinite(energy):
        raise RuntimeError('SCF did not converge. Review charge/spin/geometry and logs.')
    return mf


def dump_scf_summary(mf, dest):
    # UKS/UHF have separate alpha/beta orbitals. Save explicit spin channels.
    occ = mf.mo_occ
    energies = mf.mo_energy
    rows = []
    if isinstance(occ, (tuple, list)):
        for channel, o, e in zip(('alpha','beta'), occ, energies):
            for i, (oi, ei) in enumerate(zip(o, e), start=1):
                rows.append((channel, i, float(oi), float(ei)))
    else:
        for i, (oi, ei) in enumerate(zip(occ, energies), start=1):
            rows.append(('restricted', i, float(oi), float(ei)))
    with open(dest, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['spin_channel','orbital_index_1based','occupancy','orbital_energy_hartree'])
        writer.writerows(rows)


def save_orbitals(mf, out, cfg):
    from pyscf.tools import cubegen
    mol = mf.mol
    grid = int(cfg.get('cube_grid', 65))
    if not 24 <= grid <= 100:
        raise ValueError('cube_grid must be between 24 and 100')
    occ, coeff = mf.mo_occ, mf.mo_coeff
    channels = [('restricted', occ, coeff)]
    if isinstance(occ, (tuple, list)):
        channels = [('alpha', occ[0], coeff[0]), ('beta', occ[1], coeff[1])]
    for channel, occupations, coefficients in channels:
        occupied = np.flatnonzero(np.asarray(occupations) > 1e-8)
        unoccupied = np.flatnonzero(np.asarray(occupations) <= 1e-8)
        for label, indices in [('HOMO', occupied[-1:] ), ('LUMO', unoccupied[:1])]:
            if len(indices):
                i = int(indices[0])
                target = out / f'{channel}_{label}_{i+1}.cube'
                logging.info('Generating %s (%s orbital %s)', target, channel, i+1)
                cubegen.orbital(mol, str(target), coefficients[:,i], nx=grid,ny=grid,nz=grid)
    if cfg.get('electron_density_cube', True):
        save_density(mf, out, cfg)



def save_density(mf, out, cfg):
    """Write total electron density for restricted or unrestricted wavefunctions."""
    from pyscf.tools import cubegen
    grid = int(cfg.get('cube_grid', 65))
    if not 24 <= grid <= 100:
        raise ValueError('cube_grid must be between 24 and 100')
    dm = mf.make_rdm1()
    if isinstance(dm, (tuple, list)) or (isinstance(dm, np.ndarray) and dm.ndim == 3):
        dm = np.asarray(dm).sum(axis=0)  # alpha + beta spin populations
    logging.info('Generating total electron density cube')
    cubegen.density(mf.mol, str(out / 'electron_density.cube'), dm,
                    nx=grid, ny=grid, nz=grid)


def single_point(symbols, coords, cfg, out):
    """DFT/HF energy and optional HOMO/LUMO + density cubes at fixed geometry."""
    mol = as_pyscf_molecule(symbols, coords, cfg)
    mf = run_scf(mol, cfg)
    write_xyz(out/'geometry.xyz', symbols, coords, 'fixed input geometry')
    dump_scf_summary(mf, out/'orbitals.csv')
    (out/'single_point.json').write_text(json.dumps({
        'method':cfg.get('method','PBE0'),
        'basis':cfg.get('basis','def2-svp'),
        'energy_hartree':float(mf.e_tot),
        'geometry':'geometry.xyz'
    }, indent=2))
    if cfg.get('orbitals', True):
        save_orbitals(mf, out, cfg)
    elif cfg.get('electron_density_cube', True):
        save_density(mf, out, cfg)
    return mol, mf

def optimize(symbols, coords, cfg, out):
    from pyscf.geomopt.geometric_solver import optimize as pyscf_optimize
    mol = as_pyscf_molecule(symbols, coords, cfg)
    mf = run_scf(mol, cfg)
    logging.info('Starting full geometry optimization (geomeTRIC)')
    final_mol = pyscf_optimize(mf, maxsteps=int(cfg.get('max_opt_steps', 80)))
    final_coords = final_mol.atom_coords(unit='Angstrom')
    write_xyz(out/'optimized.xyz', symbols, final_coords, 'optimized geometry in angstrom')
    final_mf = run_scf(final_mol, cfg)
    summary = {'method': cfg.get('method','PBE0'), 'basis':cfg.get('basis','def2-svp'),
               'energy_hartree': float(final_mf.e_tot), 'scf_converged':bool(final_mf.converged),
               'optimized_xyz':'optimized.xyz'}
    (out/'optimization.json').write_text(json.dumps(summary,indent=2))
    dump_scf_summary(final_mf, out/'orbitals.csv')
    if cfg.get('orbitals', True):
        save_orbitals(final_mf, out, cfg)
    elif cfg.get('electron_density_cube', True):
        save_density(final_mf, out, cfg)
    return final_mol, final_mf


def frequency(symbols, coords, cfg, out):
    # harmonic frequencies should normally be computed at a stationary point
    mol, mf = optimize(symbols, coords, cfg, out) if cfg.get('optimize_first', True) else (
        as_pyscf_molecule(symbols, coords, cfg), None)
    if mf is None:
        mf = run_scf(mol, cfg)
    from pyscf.hessian.thermo import harmonic_analysis
    logging.info('Computing analytic harmonic Hessian')
    h = mf.Hessian().kernel()
    analysis = harmonic_analysis(mol, h, imaginary_freq=False)
    modes = np.asarray(analysis['norm_mode']).real
    freq = np.asarray(analysis['freq_wavenumber']).real
    with open(out/'frequencies.csv','w',newline='') as f:
        w = csv.writer(f)
        w.writerow(['mode_1based','frequency_cm-1','imaginary'])
        for i, value in enumerate(freq,1):
            w.writerow([i, f'{value:.5f}', value < 0])
    eq = mol.atom_coords(unit='Angstrom')
    atom_syms = [mol.atom_symbol(i) for i in range(mol.natm)]
    amplitude = float(cfg.get('vibration_amplitude_angstrom', 0.35))
    frames = int(cfg.get('vibration_frames', 16))
    if not 4 <= frames <= 80 or not 0 < amplitude <= 2:
        raise ValueError('vibration_frames 4..80 and vibration_amplitude_angstrom (0,2] required')
    for i, vec in enumerate(modes):
        scale = np.max(np.linalg.norm(vec,axis=1))
        if scale < 1e-12:
            continue
        vec = vec / scale  # normalize displayed maximum atomic displacement to amplitude
        with open(out/f'vibration_mode_{i+1:03d}.xyz','w') as f:
            for j in range(frames):
                disp = amplitude * np.sin(2*np.pi*j/frames) * vec
                f.write(f'{len(atom_syms)}\nMode {i+1}; {freq[i]:.2f} cm^-1; frame {j+1}/{frames}\n')
                for s, xyz in zip(atom_syms, eq+disp):
                    f.write(f'{s} {xyz[0]:.9f} {xyz[1]:.9f} {xyz[2]:.9f}\n')
    logging.info('Saved %s harmonic frequencies and mode animation XYZ files',len(freq))


def scan(symbols, coords, cfg, out):
    from ase import Atoms
    spec = cfg['scan']
    kind = spec['coordinate'].lower()
    length = {'distance':2, 'angle':3, 'dihedral':4}.get(kind)
    if length is None:
        raise ValueError('scan.coordinate must be distance, angle, or dihedral')
    raw = spec['atoms']
    if len(raw) != length or len(set(raw)) != length:
        raise ValueError(f'{kind} requires {length} distinct 1-based atom numbers')
    indices = [int(i)-1 for i in raw]
    if min(indices) < 0 or max(indices) >= len(symbols):
        raise ValueError('Atom indices must be within the XYZ file (1-based)')
    moved = spec.get('moved_atoms', [raw[-1]])
    if not moved or any(int(i) < 1 or int(i) > len(symbols) for i in moved):
        raise ValueError('scan.moved_atoms must contain valid 1-based atom indices')
    mask = [i+1 in moved for i in range(len(symbols))]
    if kind != 'distance' and indices[-1] not in [int(i)-1 for i in moved]:
        raise ValueError('For angles/dihedrals, scan.moved_atoms must include the final scan atom')
    if kind == 'dihedral' and any(indices[i] in [int(v)-1 for v in moved] for i in range(3)):
        raise ValueError('For dihedral scans, do not include the first three torsion atoms in moved_atoms')
    values = np.linspace(float(spec['start']),float(spec['stop']),int(spec.get('steps',11)))
    if not 2 <= len(values) <= 101:
        raise ValueError('scan.steps must be between 2 and 101')
    if kind == 'distance' and np.any(values <= 0):
        raise ValueError('All distances must be positive')
    if kind == 'angle' and np.any((values <= 0)|(values >= 180)):
        raise ValueError('Angles must be strictly between 0 and 180 degrees')
    rows = []
    with open(out/'scan_structures.xyz','w') as xyzout:
        for step, value in enumerate(values,1):
            atoms = Atoms(symbols=symbols, positions=coords.copy())
            if kind == 'distance':
                # Move the final atom, leaving other atoms unchanged; unrelaxed scan.
                atoms.set_distance(indices[0],indices[1], float(value),fix=0)
            elif kind == 'angle':
                atoms.set_angle(*indices, float(value), mask=mask)
            else:
                atoms.set_dihedral(*indices, float(value),mask=mask)
            p = atoms.get_positions()
            mol = as_pyscf_molecule(symbols, p, cfg)
            mf = run_scf(mol,cfg)
            energy = float(mf.e_tot)
            rows.append([step, float(value), energy, 0.0])
            xyzout.write(f'{len(symbols)}\n{kind}={value:.6f}; energy={energy:.12f} Eh\n')
            for s, x in zip(symbols,p):
                xyzout.write(f'{s} {x[0]:.10f} {x[1]:.10f} {x[2]:.10f}\n')
            logging.info('Scan %d/%d %s=%.4f energy=%.10f Eh',step,len(values),kind,value,energy)
    zero = min(row[2] for row in rows)
    with open(out/'scan_energies.csv','w',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['step', f'{kind}_'+('angstrom' if kind=='distance' else 'degrees'),
                         'energy_hartree','relative_energy_kj_mol'])
        for row in rows:
            row[3] = (row[2]-zero)*HARTREE_TO_KJMOL
            writer.writerow(row)
    (out/'scan_info.json').write_text(json.dumps({
        'kind':kind, 'atoms_1based':raw, 'moved_atoms_1based':moved,
        'type':'rigid / unrelaxed single-point energy scan',
        'note':'No constrained relaxation: changes are applied independently to original input geometry.',
        'units':'distance angstrom; angles degrees; energy Hartree; relative energy kJ/mol'
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,help='JSON configuration file')
    args=parser.parse_args()
    cfg_path=Path(args.config).resolve()
    cfg=json.loads(cfg_path.read_text())
    xyz_path=(cfg_path.parent/cfg['xyz']).resolve()
    output_path=(cfg_path.parent/cfg.get('output','results')).resolve()
    output_path.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.FileHandler(output_path/'run.log'), logging.StreamHandler(sys.stdout)])
    os.environ.setdefault('OMP_NUM_THREADS',os.environ.get('SLURM_CPUS_PER_TASK','1'))
    from pyscf import lib
    lib.num_threads(int(os.environ['OMP_NUM_THREADS']))
    symbols, coords=load_xyz(xyz_path)
    if len(symbols)>80:
        logging.warning('Large molecules may exceed requested memory/time; start with small structures.')
    mode=cfg.get('task','optimize').lower()
    logging.info('Task %s; atoms %d; input %s',mode,len(symbols),xyz_path)
    (output_path/'config_used.json').write_text(json.dumps(cfg,indent=2))
    if mode=='optimize':
        optimize(symbols,coords,cfg,output_path)
    elif mode=='frequency':
        frequency(symbols,coords,cfg,output_path)
    elif mode=='scan':
        scan(symbols,coords,cfg,output_path)
    elif mode in ('single_point', 'density'):
        single_point(symbols,coords,cfg,output_path)
    else:
        raise ValueError('task must be optimize, frequency, scan, single_point, or density')
    logging.info('Complete: output in %s',output_path)


if __name__=='__main__':
    main()
