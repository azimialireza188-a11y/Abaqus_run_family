"""Independent rounded rectangular HSS member comparison, N/mm/MPa.

Geometry is calculated from family CSV inputs; a legacy report is optional. The continuous closed shell has
no built-up seams, bolts or transverse plates. Imperfections use this HSS's own
classified compression-buckling REF_L and REF_G modes; no built-up eigenvector
or analytical local shape is substituted. No native analysis runs on import,
preflight, or --dry-run. Actual execution requires the Abaqus launcher.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

SCRIPT_DIR = Path(globals().get('__file__', sys._getframe().f_code.co_filename)).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import abaqus_load_case as loads
import abaqus_pm_interaction as pm
from abaqus_report_reference import _Workbook, _header, _text, _positive


def user_arguments(argv=None):
    """Separate CAE kernel options from our strict, double-dash user options.

    CAE 2024 can remove the '--' separator and append its own options after
    --native-inputs. Do not use parse_known_args: user misspellings must fail.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    kernel = any(arg == '-cae' or arg == '-noGUI' or arg.startswith('-noGUI=') for arg in argv)
    if '--' in argv:
        argv = argv[argv.index('--')+1:]
    if not kernel:
        return argv
    clean, index = [], 0
    valued = ('-noGUI', '-tmpdir', '-lmlog')
    while index < len(argv):
        token = argv[index]
        if token == '-cae':
            index += 1
        elif token in valued:
            if index+1 >= len(argv) or argv[index+1].startswith('-'):
                raise ValueError('Missing value for Abaqus kernel option '+token)
            index += 2
        elif any(token.startswith(flag+'=') for flag in valued):
            index += 1
        else:
            clean.append(token); index += 1
    return clean


def parse_arguments(argv=None):
    argv = user_arguments(argv)
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for flag in ('family-root', 'output-root'):
        p.add_argument('--'+flag, required=True)
    p.add_argument('--cufsm-report-dir', help='Optional legacy Excel geometry override')
    p.add_argument('--hss-radius-mm', type=float, help='Centre-line corner radius; otherwise use member CSV bend metadata')
    p.add_argument('--axes', default='0')
    p.add_argument('--pm-angles', default='0,15,30,45,60,75,90')
    p.add_argument('--pm-directory', default='Step5_PM_HSS',
                   help='Fresh nonlinear folder under each HSS axis; shared references are reused')
    p.add_argument('--model-cases', choices=('perfect', 'imperfect', 'imperfection', 'both'), default='both')
    p.add_argument('--mesh-mm', type=float, default=20.)
    p.add_argument('--fy', type=float, required=True)
    p.add_argument('--n-modes', type=int, default=250)
    p.add_argument('--n-vectors', type=int, default=500)
    p.add_argument('--max-iterations', type=int, default=1250)
    p.add_argument('--local-mode', type=int, default=1)
    p.add_argument('--global-modes', default='1')
    p.add_argument('--cpus', type=pm.runtime_resources.cpu_request, default='auto')
    from abaqus_step5_queue import parallel_request
    p.add_argument('--parallel-jobs', type=parallel_request, default='auto')
    p.add_argument('--abaqus-cmd', default='abaqus')
    p.add_argument('--riks-extrapolation', choices=('linear', 'none'), default='linear')
    p.add_argument('--riks-controls', choices=('auto', 'default', 'robust'), default='auto',
                   help='Auto: bounded Newton controls for new jobs; preserve saved controls on resume')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--collect-only', action='store_true')
    args = p.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.pm_directory):
        p.error('--pm-directory must be a single alphanumeric folder name (underscores/hyphens allowed)')
    if args.model_cases == 'imperfect':
        args.model_cases = 'imperfection'
    if args.hss_radius_mm is not None:
        _positive(args.hss_radius_mm, 'hss-radius-mm')
        if args.cufsm_report_dir:
            p.error('--hss-radius-mm cannot override an explicit report geometry')
    for name in ('mesh_mm', 'fy'):
        _positive(getattr(args, name), name)
    if min(args.n_modes, args.n_vectors, args.max_iterations, args.local_mode) < 1 or args.n_vectors < args.n_modes:
        p.error('Positive mode/vector/iteration counts required; vectors must be >= modes')
    try:
        args.global_modes = [int(value) for value in str(args.global_modes).split(',')]
    except ValueError:
        p.error('--global-modes must contain positive integer mode numbers')
    if not args.global_modes or min(args.global_modes) < 1 or len(set(args.global_modes)) != len(args.global_modes):
        p.error('--global-modes must contain distinct positive mode numbers')
    if max([args.local_mode]+args.global_modes) > args.n_modes:
        p.error('Selected mode exceeds --n-modes')
    args.pm_angles = sorted(set(pm.parse_float_list(args.pm_angles, '--pm-angles')))
    if any(not 0. <= value <= 90. for value in args.pm_angles):
        p.error('--pm-angles must lie between 0 and 90 degrees')
    return args


def read_hss_report(directory, fy):
    path = Path(directory).resolve()/'summary.xlsx'
    with zipfile.ZipFile(path) as archive:
        workbook = _Workbook(archive)
        rows = workbook.rows('section_properties_3')
        start, columns = _header(rows, ('property', 'unit', 'box_same_A'), 'section_properties_3')
        wanted = {'outside-to-outside width B (along x)': ('B_mm', 'mm'),
                  'outside-to-outside depth H (along z)': ('H_mm', 'mm'),
                  'A': ('area_report_mm2', 'mm^2'), 'S_x = I / c_max': ('Sx_mm3', 'mm^3'),
                  'S_z = I / c_max': ('Sz_mm3', 'mm^3'),
                  'Z_x (plastic modulus)': ('Zx_mm3', 'mm^3'),
                  'Z_z (plastic modulus)': ('Zz_mm3', 'mm^3')}
        result = {}
        for row in rows[start+1:]:
            item = _text(row.get(columns['property']))
            if item not in wanted:
                continue
            key, unit = wanted[item]
            if key in result or _text(row.get(columns['unit'])) != unit:
                raise ValueError('Duplicate property or invalid unit for HSS '+item)
            result[key] = _positive(row.get(columns['box_same_A']), 'box_same_A '+item)
        if len(result) != len(wanted):
            raise ValueError('Missing exact HSS dimensions/properties in section_properties_3 / box_same_A')
        rows = workbook.rows('key_values')
        start, columns = _header(rows, ('group', 'item', 'value', 'unit'), 'key_values')
        wanted = {('input', 't'): 't_mm', ('input', 'member length L'): 'length_mm',
                  ('box', 'corner R (centreline)'): 'radius_mm'}
        for row in rows[start+1:]:
            pair = (_text(row.get(columns['group'])), _text(row.get(columns['item'])))
            if pair not in wanted:
                continue
            key = wanted[pair]
            if key in result or _text(row.get(columns['unit'])) != 'mm':
                raise ValueError('Duplicate HSS dimension or invalid unit: '+str(pair))
            result[key] = _positive(row.get(columns['value']), str(pair))
        if not set(wanted.values()).issubset(result):
            raise ValueError('Missing HSS t, L or centreline corner radius in key_values')
    result.update(fy_MPa=float(fy), source_workbook=str(path), source_sha256=sha256(path), group='box_same_A')
    if min(result['B_mm'], result['H_mm']) <= result['t_mm']+2.*result['radius_mm']:
        raise ValueError('HSS dimensions do not permit the reported rounded centreline corners')
    if result['radius_mm'] <= .5*result['t_mm']:
        raise ValueError('HSS inner corner radius must be positive')
    return result


def hss_from_family(directory, fy, radius=None):
    """Same nominal-area rounded square used by the report generator.

    A0 is the requested equal area, not the slightly shorter chord perimeter.
    B = A0/(4t) + (2-pi/2)R + t; R is a centre-line radius. No product
    thickness reduction and no default radius assumption are introduced.
    """
    from abaqus_report_reference import read_family_geometry
    from abaqus_hss_aisc import rounded_properties
    data = read_family_geometry(directory)
    t, member = data['thickness_mm'], data['member']
    area = (_positive(member['A0_mm2'],'nominal equal area A0_mm2')
            if member.get('A0_mm2','').strip() else loads.section_properties(data['segments'],t)['area_mm2'])
    radius_basis = 'explicit --hss-radius-mm'
    if radius is None:
        for key in ('hss_radius_mm','box_radius_mm','bend_radius_mm'):
            if member.get(key,'').strip():
                radius = _positive(member[key],key); radius_basis = 'member CSV '+key
                break
    if radius is None:
        # This is structured member metadata, not a guess from the folder name.
        matches = re.findall(r'(?:^|_)R([0-9]+(?:[p.][0-9]+)?)(?=_|$)',member.get('tag',''))
        if len(matches) != 1:
            raise ValueError('HSS centre-line radius is missing/ambiguous in member CSV; supply --hss-radius-mm')
        radius = _positive(matches[0].replace('p','.'),'member bend R/t')*t
        radius_basis = 'member CSV tag bend R/t times actual thickness'
    radius = _positive(radius,'HSS centre-line radius')
    B = area/(4.*t)+(2.-math.pi/2.)*radius+t
    props = rounded_properties(B,B,t,radius)
    result = dict(B_mm=B,H_mm=B,t_mm=t,radius_mm=radius,length_mm=data['length_mm'],
                  E_MPa=data['E_MPa'],nu=data['nu'],fy_MPa=float(fy),area_report_mm2=area,
                  group='box_same_A',source='family CSV geometry',radius_basis=radius_basis,
                  area_basis='member A0_mm2' if member.get('A0_mm2','').strip() else 'four-piece t*ds area')
    result.update({key:props[key] for key in ('Sx_mm3','Sz_mm3','Zx_mm3','Zz_mm3')})
    result['source_sha256'] = hashlib.sha256(json.dumps(data,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()
    return result


def same_hss_geometry(first, second):
    keys = ('B_mm','H_mm','t_mm','radius_mm','length_mm','E_MPa','nu','fy_MPa')
    return all(math.isclose(float(first[key]),float(second[key]),rel_tol=1e-10,abs_tol=1e-10)
               for key in keys)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_riks_settings(args, settings=None):
    """Bind numerical controls to each immutable nonlinear input, not the mesh."""
    from abaqus_step5_gmnia import riks_controls_profile
    if settings is not None and settings.get('riks_extrapolation','linear') != args.riks_extrapolation:
        raise ValueError('Existing HSS Riks extrapolation differs; use a fresh --pm-directory')
    return riks_controls_profile(getattr(args,'riks_controls','auto'),settings)


def preflight(args):
    """Read-only plan for the comparison runner; never creates output directories."""
    import abaqus_complete_model_m20 as builder
    cfg = read_hss_report(args.cufsm_report_dir, args.fy) if args.cufsm_report_dir else None
    root = Path(args.family_root).expanduser().resolve()
    families = sorted({path.parent for path in root.rglob('builtup_segments.csv')})
    if (root/'builtup_segments.csv').is_file():
        families = sorted(set(families) | {root})
    if not families:
        raise ValueError('No family builtup_segments.csv found: '+str(root))
    elastic = None
    for family in families:
        if not args.cufsm_report_dir:
            candidate = hss_from_family(family,args.fy,getattr(args,'hss_radius_mm',None))
            if cfg is not None and not same_hss_geometry(cfg,candidate):
                raise ValueError('Families have different HSS geometry/material; one HSS benchmark would be ambiguous')
            cfg = candidate
        unused, thickness, young, poisson = builder.read_geometry(str(family))
        with (family/'builtup_member.csv').open(encoding='utf-8-sig') as stream:
            members = list(csv.DictReader(stream))
        if len(members) != 1:
            raise ValueError('Expected one member row: '+str(family))
        length = _positive(members[0].get('L_mm'), 'family member length')
        if not math.isclose(length, cfg['length_mm'], rel_tol=1e-9) or not math.isclose(thickness, cfg['t_mm'], rel_tol=1e-9):
            raise ValueError('HSS report t/L differs from family CSV: '+str(family))
        current = (young, poisson)
        if elastic is not None and any(not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) for a, b in zip(elastic, current)):
            raise ValueError('Families have different E/nu; one HSS benchmark would be ambiguous')
        elastic = current
    cfg.update(E_MPa=elastic[0], nu=elastic[1])
    # Provenance alone must not change old inputs/sidecars on --resume. Match
    # every physical dimension/material first; never open the old workbook.
    saved_plan = Path(args.output_root).expanduser().resolve()/'hss_pipeline.json'
    if not args.cufsm_report_dir and saved_plan.is_file():
        saved_cfg = json.loads(saved_plan.read_text(encoding='utf-8-sig'))['plan']['geometry']
        if not same_hss_geometry(cfg,saved_cfg):
            raise ValueError('Existing HSS geometry/material differs from family CSV; use a new output root')
        cfg = saved_cfg
    mesh = make_mesh(cfg, args.mesh_mm)
    spec = args.axes if args.axes in ('required', 'all') else pm.parse_float_list(args.axes, '--axes')
    axes, unused = pm.resolve_axes(spec, mesh['segments'], check_symmetry=False)
    out = Path(args.output_root).expanduser().resolve()
    directories = [dict(pm_dir=str(out/('HSS_Axis'+loads.angle_token(axis))/args.pm_directory),
                        label='HSS', axis_deg=axis) for axis in axes]
    plan = dict(geometry=cfg, pm_dirs=directories, families=[str(path) for path in families],
                imperfect_reference='own HSS compression REF_L + restrained-twist flexural REF_G',
                imperfections='a_L=0.34t normal displacement; a_G=L/1000 transverse displacement',
                buckling_reference_modes=args.n_modes)
    preserve_pipeline_plan(out/'hss_pipeline.json', pipeline_request(args, plan), check_only=True)
    for entry in directories:
        for sidecar in Path(entry['pm_dir']).glob('PM_*_step5.json'):
            saved = json.loads(sidecar.read_text(encoding='utf-8-sig'))
            validate_riks_settings(args,saved.get('settings', {}))
    return plan


def make_mesh(cfg, target):
    """A conforming periodic S4R shell mesh; every corner retains >=8 chords."""
    target = _positive(target, 'mesh-mm')
    b = (cfg['B_mm']-cfg['t_mm'])/2.
    h = (cfg['H_mm']-cfg['t_mm'])/2.
    radius = cfg['radius_mm']
    if radius <= 0 or radius >= min(b, h):
        raise ValueError('Invalid HSS centreline radius')
    points = []
    corners = [(b-radius, h-radius, 0.), (-b+radius, h-radius, 90.),
               (-b+radius, -h+radius, 180.), (b-radius, -h+radius, 270.)]
    count = max(8, int(math.ceil(math.pi*radius/(2.*target))))
    for k, (cx, cy, start) in enumerate(corners):
        for i in range(count+1):
            a = math.radians(start+90.*i/count)
            points.append((cx+radius*math.cos(a), cy+radius*math.sin(a)))
        nx, ny, na = corners[(k+1) % 4]
        end = (nx+radius*math.cos(math.radians(na)), ny+radius*math.sin(math.radians(na)))
        first = points[-1]
        divisions = max(1, int(math.ceil(math.hypot(end[0]-first[0], end[1]-first[1])/target)))
        for i in range(1, divisions):
            f = i/float(divisions)
            points.append((first[0]*(1.-f)+end[0]*f, first[1]*(1.-f)+end[1]*f))
    n = len(points)
    segments = [points[i]+points[(i+1) % n] for i in range(n)]
    normals = []
    for i in range(n):
        a, p, c = points[(i-1) % n], points[i], points[(i+1) % n]
        incoming = (p[0]-a[0], p[1]-a[1]); outgoing = (c[0]-p[0], c[1]-p[1])
        u = math.hypot(*incoming); v = math.hypot(*outgoing)
        tx, ty = incoming[0]/u+outgoing[0]/v, incoming[1]/u+outgoing[1]/v
        scale = math.hypot(tx, ty)
        normals.append((ty/scale, -tx/scale))
    # Even longitudinal interval count preserves an exact midspan anchor ring.
    nz = max(2, int(math.ceil(cfg['length_mm']/target)))
    nz += nz % 2
    z = [cfg['length_mm']*i/nz for i in range(nz+1)]
    nodes = [(x, y, zi) for zi in z for x, y in points]
    elements = [(j*n+i+1, j*n+(i+1) % n+1, (j+1)*n+(i+1) % n+1, (j+1)*n+i+1)
                for j in range(nz) for i in range(n)]
    area = loads.section_properties(segments, cfg['t_mm'])['area_mm2']
    expected_area = cfg['area_report_mm2']
    if cfg.get('source') == 'family CSV geometry':
        # The shell uses straight chords; the requested equal area uses exact
        # circular corners. Account for that known discretization exactly.
        expected_area -= cfg['t_mm']*(2.*math.pi*radius-8.*count*radius*math.sin(math.pi/(4.*count)))
    if not math.isclose(area, expected_area, rel_tol=5e-4, abs_tol=1e-5):
        raise ValueError('Constructed HSS shell area differs from box_same_A report (%.12g vs %.12g)' %
                         (area, cfg['area_report_mm2']))
    return dict(section=points, segments=segments, normals=normals, z=z, nodes=nodes,
                elements=elements, area_mm2=area, anchor=(nz//2)*n+1)


def loading(mesh, cfg, axis, angle, reference_stress=None):
    definition = loads.combined_reference(mesh['segments'], cfg['t_mm'], axis, angle)
    scale = cfg['fy_MPa'] if reference_stress is None else reference_stress
    n = len(mesh['section']); top_offset = n*(len(mesh['z'])-1)
    coordinates = {i+1: point for i, point in enumerate(mesh['section'])}
    edges = [(i+1, (i+1) % n+1) for i in range(n)]
    force = loads.consistent_edge_forces(coordinates, edges, cfg['t_mm'], definition)
    bottom = {label: scale*value for label, value in force.items()}
    top = {top_offset+label: -value for label, value in bottom.items()}
    nx, ny = definition['compression_normal']; xc, yc = definition['centroid_mm']
    axial = sum(bottom.values())
    moment = sum(value*(nx*(coordinates[label][0]-xc)+ny*(coordinates[label][1]-yc))
                 for label, value in bottom.items())
    if angle == 90.:
        if abs(axial) > 1e-9*cfg['fy_MPa']*mesh['area_mm2']:
            raise ValueError('Pure HSS bending contains an axial resultant')
        axial = 0.
    if angle == 0.:
        moment = 0.
    return dict(definition=definition, bottom=bottom, top=top,
                axial_force_N=axial, moment_Nmm=moment, reference_stress=scale)


def input_text(mesh, cfg, pattern, nonlinear=False, coordinates=None, n_modes=20,
               n_vectors=60, max_iterations=300, extrapolation='linear', riks_controls='default'):
    if extrapolation not in ('linear','none'):
        raise ValueError('Unsupported Riks extrapolation: '+str(extrapolation))
    n = len(mesh['section']); top = n*(len(mesh['z'])-1)
    xyz = mesh['nodes'] if coordinates is None else coordinates
    out = ['*Heading', '** Independent continuous HSS / box_same_A; N, mm, MPa',
           '*Part, name=HSS', '*Node']
    out += ['%d, %.17g, %.17g, %.17g' % ((i+1,)+tuple(point)) for i, point in enumerate(xyz)]
    out += ['*Element, type=S4R, elset=WALL']
    out += [', '.join(str(value) for value in (i+1,)+element) for i, element in enumerate(mesh['elements'])]
    out += ['*Shell Section, elset=WALL, material=STEEL', '%.17g, 5' % cfg['t_mm'], '*End Part',
            '*Assembly, name=ASSEMBLY', '*Instance, name=HSS, part=HSS', '*End Instance',
            '*Nset, nset=STEP5_BOTTOM, instance=HSS, generate', '1, %d, 1' % n,
            '*Nset, nset=STEP5_TOP, instance=HSS, generate', '%d, %d, 1' % (top+1, top+n),
            '*Nset, nset=ENDS', 'STEP5_BOTTOM, STEP5_TOP',
            '*Nset, nset=AXIAL_ANCHOR, instance=HSS', str(mesh['anchor']), '*End Assembly',
            '*Material, name=STEEL', '*Elastic', '%.17g, %.17g' % (cfg['E_MPa'], cfg['nu'])]
    if nonlinear:
        out += ['*Plastic', '%.17g, 0.' % cfg['fy_MPa']]
    out += ['*Boundary', 'ENDS, 1, 2, 0.', 'ENDS, 6, 6, 0.', 'AXIAL_ANCHOR, 3, 3, 0.']
    if nonlinear:
        out += ['*Step, name=GMNIA, nlgeom=YES, inc=5000, extrapolation='+('NO' if extrapolation=='none' else 'LINEAR'),
                '*Static, riks', '0.01, 1., 1.e-8, 0.05']
        from abaqus_step5_gmnia import riks_controls_keywords
        out += riks_controls_keywords(riks_controls).splitlines()
    else:
        out += ['*Step, name=Buckle, perturbation', '*Buckle, eigensolver=SUBSPACE',
                '%d, , %d, %d' % (n_modes, n_vectors, max_iterations)]
    out += ['*Cload']
    out += ['HSS.%d, 3, %.17g' % (label, value) for end in ('bottom', 'top')
            for label, value in sorted(pattern[end].items()) if value != 0.]
    out += ['*Output, field, frequency=1', '*Node Output', 'U, UR, RF']
    if nonlinear:
        out += ['*Element Output', 'S, LE, PEEQ', '*Output, history, variable=PRESELECT']
    out += ['*End Step', '']
    return '\n'.join(out)


def validate_mode(rows, mode, kind):
    matches = [row for row in rows if int(row['mode']) == mode]
    if len(matches) != 1:
        raise ValueError('Own HSS REF_%s classification lacks selected mode %d' % (kind, mode))
    row = matches[0]
    try:
        values = {key: float(row[key+'_pct']) for key in ('G', 'D', 'L', 'C', 'O')}
        eigenvalue = float(row['eigenvalue'])
    except (KeyError, ValueError, TypeError):
        raise ValueError('Incomplete own HSS mode purity evidence') from None
    if (not all(math.isfinite(value) and 0 <= value <= 100 for value in values.values())
            or not math.isclose(sum(values.values()), 100., rel_tol=0., abs_tol=1e-3)
            or not math.isfinite(eigenvalue) or eigenvalue <= 0
            or values[kind] < 95. or values['O'] > 5.):
        raise ValueError('Own HSS REF_%s mode %d does not pass 95%% purity / 5%% Other checks' % (kind, mode))
    lower = row.get('subspace_'+kind+'_min_pct')
    if lower not in (None, ''):
        lower = float(lower)
        if not math.isfinite(lower) or lower < 95. or lower > 100.:
            raise ValueError('Selected own HSS eigenspace does not pass 95% purity')
    return row


def imperfect_coordinates(mesh, cfg, local, global_mode, axis):
    import numpy as np
    xyz = np.asarray(mesh['nodes'], dtype=float)
    local = np.array(local, dtype=float, copy=True); global_mode = np.array(global_mode, dtype=float, copy=True)
    if local.shape != xyz.shape or global_mode.shape != xyz.shape or not np.all(np.isfinite(local+global_mode)):
        raise ValueError('Own HSS mode vectors are incomplete/nonfinite or use a different mesh')
    # Match the existing Step4 convention: only transverse translations define
    # the stress-free initial geometry; axial modal U and shell UR are excluded.
    local[:, 2] = 0.; global_mode[:, 2] = 0.
    n = len(mesh['section'])
    normals = np.tile(np.asarray(mesh['normals']), (len(mesh['z']), 1))
    normal_displacement = np.sum(local[:, :2]*normals, axis=1)
    amplitude_l = np.max(np.abs(normal_displacement))
    amplitude_g = np.max(np.linalg.norm(global_mode[:, :2], axis=1))
    if amplitude_l <= 1e-12 or amplitude_g <= 1e-12:
        raise ValueError('Selected own HSS mode has zero local-normal/global-transverse amplitude')
    # Keep actual eigenvector direction. Do not rotate a nondegenerate mode or
    # assume a square section from the report's rounded printed side length.
    normal = np.array([-math.sin(math.radians(axis)), math.cos(math.radians(axis))])
    def phase(vector):
        response = vector[:, :2].dot(normal)
        if np.max(np.abs(response)) <= 1e-10*np.max(np.linalg.norm(vector[:, :2], axis=1)):
            response = vector[:, int(np.argmax(np.max(np.abs(vector[:, :2]), axis=0)))]
        return 1. if response[int(np.argmax(np.abs(response)))] >= 0 else -1.
    a_l, a_g = -.34*cfg['t_mm'], cfg['length_mm']/1000.
    phase_l = 1. if normal_displacement[int(np.argmax(np.abs(normal_displacement)))] >= 0 else -1.
    phase_g = phase(global_mode)
    correction = phase_l*a_l/amplitude_l*local+phase_g*a_g/amplitude_g*global_mode
    support = np.r_[np.arange(n), np.arange(len(xyz)-n, len(xyz))]
    if np.max(np.abs(correction[support, :2])) > 1e-5*max(a_l, a_g):
        raise ValueError('Selected HSS modes violate end transverse supports')
    correction[support] = 0.
    anchor = mesh['anchor']-1
    if abs(correction[anchor, 2]) > 1e-5*max(a_l, a_g):
        raise ValueError('Selected HSS modes violate the middle axial anchor')
    correction[anchor, 2] = 0.
    return (xyz+correction).tolist(), dict(a_L_mm=a_l, a_D_mm=0., a_G_mm=a_g,
        local_component_mm=a_l, distortional_component_mm=0., global_component_mm=a_g,
        local_phase_multiplier=phase_l, global_phase_multiplier=phase_g,
        normalization_L='maximum wall-normal translation of the OWN HSS REF_L eigenvector',
        normalization_G='maximum transverse translation of the OWN HSS REF_G eigenvector',
        geometry_components='transverse translations only, as in the existing Step4 workflow',
        direction='actual HSS eigenspace; sign phased toward requested compression normal')


def stable_write(path, text):
    """Resume never replaces a generated physical model with a different one."""
    path = Path(path)
    if path.exists():
        if path.read_text(encoding='utf-8') != text:
            raise ValueError('Existing HSS input/configuration differs; select a new output root: '+str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def stable_json(path, value):
    stable_write(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n')


def pipeline_request(args, plan):
    return dict(plan=plan, mesh_mm=args.mesh_mm, n_modes=args.n_modes,
                n_vectors=args.n_vectors, max_iterations=args.max_iterations, local_mode=args.local_mode,
                global_modes=args.global_modes, model_cases=args.model_cases, pm_angles=args.pm_angles,
                riks_extrapolation=args.riks_extrapolation)


def preserve_pipeline_plan(path, request, check_only=False):
    """A fresh nonlinear folder may share the same immutable HSS base model."""
    if path.is_file():
        previous = json.loads(path.read_text(encoding='utf-8-sig'))
        def base(value):
            value = dict(value, plan=dict(value['plan']))
            value['plan'].pop('pm_dirs', None)
            # The predictor is recorded/checked on every PM input sidecar.
            # A fresh nonlinear folder can reuse the unchanged elastic mesh.
            value.pop('riks_extrapolation', None)
            return value
        if base(previous) != base(request):
            raise ValueError('Existing HSS base model/configuration differs; use a new output root: '+str(path))
    elif not check_only:
        stable_json(path, request)


def command(args, values, cwd, log_path=None):
    print('[HSS] '+subprocess.list2cmdline(values), flush=True)
    request = subprocess.list2cmdline(values) if os.name == 'nt' else values
    if log_path is None:
        from abaqus_step5_progress import console_bridge
        with console_bridge(cwd, os.environ) as environment:
            code = subprocess.call(request, cwd=str(cwd), shell=os.name == 'nt', env=environment)
    else:
        print('[HSS] CAE LOG: '+str(log_path), flush=True)
        with Path(log_path).open('w', encoding='utf-8') as stream:
            code = subprocess.call(request, cwd=str(cwd), shell=os.name == 'nt',
                                   stdout=stream, stderr=subprocess.STDOUT)
    if code:
        detail = '\n'+native_diagnostics(cwd, log_path) if log_path else ''
        raise RuntimeError('HSS subprocess failed with exit code %d' % code+detail)


def native_diagnostics(directory, log_path=None):
    """Bounded read of native diagnostics, including CAE's zero-exit parse errors."""
    details = []
    paths = ([Path(log_path)] if log_path else [])+[Path(directory)/'abaqus.rpy']
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open('rb') as stream:
                stream.seek(0, 2); stream.seek(max(0, stream.tell()-8000))
                text = stream.read().decode('utf-8', errors='replace').strip()
        except OSError as error:
            details.append(str(path)+': cannot read native diagnostics: '+str(error))
            continue
        if text:
            details.append(str(path)+':\n'+text)
    return '\n'.join(details) or 'No native diagnostic text was written.'


def own_modes(args, axis_dir, mesh, cfg, axis):
    """Build, solve, export and classify only independent HSS reference models."""
    source = axis_dir/'HSS_reference'
    source.mkdir(parents=True, exist_ok=True)
    inp = source/'HSS_reference.inp'
    pattern = loading(mesh, cfg, 0., 0., reference_stress=1.)
    text = input_text(mesh, cfg, pattern, n_modes=args.n_modes, n_vectors=args.n_vectors,
                      max_iterations=args.max_iterations)
    stable_write(inp, text)
    stable_json(source/'HSS_reference_build.json', dict(job_name='HSS_reference',
        load_case=dict(type='compression', reference_stress_MPa=1.),
        source_inputs=dict(thickness_mm=cfg['t_mm'], length_mm=cfg['length_mm'],
                           E_MPa=cfg['E_MPa'], nu=cfg['nu']), input_sha256=sha256(inp)))
    script = SCRIPT_DIR/'gdlc_classifier'/'gdlc_mode_decomposition.py'
    command(args, [args.abaqus_cmd, 'python', str(script), str(source), '--make-reference-models',
                   '--reference-modes', str(args.n_modes), '--reference-kinds', 'G,L',
                   '--ref-g-twist', 'restrained', '--export', 'never'], axis_dir)
    references = {kind: axis_dir/('HSS_reference_REF_'+kind) for kind in ('L', 'G')}
    cache_path = axis_dir/'hss_reference_classification_cache.json'
    signature = classification_signature(references, script)
    prior = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.is_file() else None
    if signature is None or signature != prior:
        command(args, [args.abaqus_cmd, 'python', str(script)]+[str(references[key]) for key in ('L', 'G')]+
                ['--solve-reference-models', '--export', 'auto', '--stiffness', 'never', '--plots', 'none',
                 '--cpus', str(args.cpus), '--abaqus-cmd', args.abaqus_cmd], axis_dir)
        signature = classification_signature(references, script)
        if signature is None:
            raise ValueError('HSS classification did not produce complete current reference evidence')
        cache_path.write_text(json.dumps(signature, indent=2, sort_keys=True), encoding='utf-8')
    import abaqus_step4_imperfections as step4
    result = {}
    evidence = {}
    for kind, directory in references.items():
        with (directory/'mode_decomposition_GDLC'/'mode_participation.csv').open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        eigenvalues = {int(row['mode']): float(row['eigenvalue']) for row in rows}
        groups = {}
        if kind == 'G':
            for mode in args.global_modes:
                group, note = step4.global_modes_for_angle((mode,), eigenvalues, True)
                groups[mode] = (group, note)
            selected = sorted({mode for group, unused in groups.values() for mode in group})
        else:
            selected = [args.local_mode]
        for mode in selected:
            evidence[kind+str(mode)] = validate_mode(rows, mode, kind)
        # Export selection is bound to this exact HSS reference input and ODB.
        candidates = []
        for path in directory.glob('modal_export*/modal_export.json'):
            meta = json.loads(path.read_text(encoding='utf-8-sig'))
            job = 'HSS_reference_REF_'+kind
            if meta.get('source_inp_sha256') == sha256(directory/(job+'.inp')) and meta.get('source_odb_sha256') == sha256(directory/(job+'.odb')):
                candidates.append((path.parent, meta))
        if not candidates:
            raise ValueError('No current, hash-verified own HSS reference modal export: '+str(directory))
        export, meta = candidates[-1]
        verify_export_artifacts(export, meta)
        for mode in selected:
            result[kind+str(mode)] = read_mode_vector(export, meta, mode, len(mesh['nodes']))
        if kind == 'G':
            vectors = dict(result)
            for mode, (group, note) in groups.items():
                # A repeated eigenvalue permits combinations within that OWN
                # HSS eigenspace, not arbitrary rotation of a single mode.
                shape, diagnostic = step4.global_eigen_shape(
                    [vectors['G'+str(member)] for member in group], 2, axis+90., check_direction=True)
                result['G'+str(mode)] = shape
                evidence['G%d_direction' % mode] = dict(diagnostic, effective_modes=list(group), note=note)
        evidence[kind+'_source'] = dict(directory=str(directory), input_sha256=meta['source_inp_sha256'],
            odb_sha256=meta['source_odb_sha256'], modal_export_sha256=sha256(export/'modal_export.json'),
            classification_sha256=sha256(directory/'mode_decomposition_GDLC'/'mode_participation.csv'),
            classifier_sha256=sha256(script))
    return result, evidence


def classification_signature(references, script):
    result = dict(classifier_sha256=sha256(script), stiffness_sha256=sha256(script.with_name('gdlc_stiffness.py')))
    for kind, directory in references.items():
        paths = [directory/('HSS_reference_REF_'+kind+'.inp'),
                 directory/('HSS_reference_REF_'+kind+'.odb'),
                 directory/'mode_decomposition_GDLC'/'mode_participation.csv',
                 directory/'mode_decomposition_GDLC'/'basis_report.json']
        if any(not path.is_file() for path in paths) or list(directory.glob('*.lck')):
            return None
        result[kind] = {path.name: sha256(path) for path in paths}
    return result


def verify_export_artifacts(directory, metadata):
    required = ['raw_dof_map.npz']+list(metadata.get('shards') or [])
    hashes = metadata.get('artifact_sha256') or {}
    if len(required) < 2 or not metadata.get('source_stability_verified'):
        raise ValueError('Own HSS export lacks stable-source/artifact verification')
    for name in required:
        if Path(name).name != name or not hashes.get(name) or sha256(Path(directory)/name) != hashes[name]:
            raise ValueError('Own HSS export artifact is missing or changed: '+str(name))


def read_mode_vector(export, meta, mode, node_count):
    import numpy as np
    with np.load(Path(export)/'raw_dof_map.npz', allow_pickle=False) as mapping:
        instances = np.char.upper(mapping['instances'].astype(str))
        labels = mapping['labels'].astype(int); dofs = mapping['dofs'].astype(int)
    required = {(label, dof) for label in range(1, node_count+1) for dof in (1, 2, 3)}
    vector = np.full((node_count, 3), np.nan)
    found = False
    for shard in meta['shards']:
        with np.load(Path(export)/shard, allow_pickle=False) as data:
            modes = data['modes'].astype(int).tolist()
            if mode not in modes:
                continue
            if found or len(modes) != len(set(modes)):
                raise ValueError('Duplicate selected HSS mode in export')
            values = data['vectors'][:, modes.index(mode)]
            if len(values) != len(labels):
                raise ValueError('HSS mode DOF-map mismatch')
            seen = set()
            for inst, label, dof, value in zip(instances, labels, dofs, values):
                if dof > 3:
                    continue
                key = (int(label), int(dof))
                if inst != 'HSS' or key not in required or key in seen:
                    raise ValueError('HSS mode labels/instance mismatch or duplicate DOF')
                vector[label-1, dof-1] = value; seen.add(key)
            if seen != required:
                raise ValueError('Incomplete own HSS eigenvector')
            found = True
    if not found or not np.all(np.isfinite(vector)):
        raise ValueError('Missing/nonfinite own HSS eigenvector')
    return vector


def prepare_points(args, directory, mesh, cfg, axis, modes=None, evidence=None):
    directory.mkdir(parents=True, exist_ok=True)
    section = pm.section_reference(mesh['segments'], cfg['t_mm'], [axis], args.fy)
    section['section_label']='HSS'
    stable_json(directory/pm.SECTION_FILE, section)
    n = len(mesh['section']); offset = n*(len(mesh['z'])-1)
    coords = {i+1: point for i, point in enumerate(mesh['section'])}
    edges = [(i+1, (i+1) % n+1) for i in range(n)]
    area = loads.consistent_edge_forces(coords, edges, cfg['t_mm'], loads.combined_reference(mesh['segments'], cfg['t_mm'], axis, 0.))
    bend = loading(mesh, cfg, axis, 90., reference_stress=1.)
    area_weights = [[['HSS', label+side*offset, value] for label, value in sorted(area.items())] for side in (0, 1)]
    rotation = [[['HSS', label, value/bend['moment_Nmm']] for label, value in sorted(bend[end].items()) if value != 0.]
                for end in ('bottom', 'top')]
    cases = []
    if args.model_cases in ('perfect', 'both'):
        cases.append(('PERFECT', None, dict(a_L_mm=0., a_D_mm=0., a_G_mm=0.,
                      local_component_mm=0., distortional_component_mm=0., global_component_mm=0.)))
    if args.model_cases in ('imperfection', 'both'):
        for mode in args.global_modes:
            coordinates, provenance = imperfect_coordinates(mesh, cfg, modes['L'+str(args.local_mode)], modes['G'+str(mode)], axis)
            provenance.update(local_mode=args.local_mode, global_mode=mode, own_HSS_reference_evidence=evidence)
            label = 'LG_mp' if len(args.global_modes) == 1 else 'LG_mp_G%d' % mode
            cases.append((label, coordinates, provenance))
    jobs = []
    for case, coordinates, provenance in cases:
        for stem, unused, angle in pm.ray_list([axis], args.pm_angles):
            name = stem+'_'+case
            sidecar = directory/(name+'_step5.json')
            saved_settings = (json.loads(sidecar.read_text(encoding='utf-8'))['settings']
                              if sidecar.is_file() else None)
            controls = validate_riks_settings(args,saved_settings)
            pattern = loading(mesh, cfg, axis, angle)
            text = input_text(mesh, cfg, pattern, nonlinear=True, coordinates=coordinates,
                              extrapolation=args.riks_extrapolation, riks_controls=controls)
            path = directory/(name+'.inp'); stable_write(path, text)
            ref = loads.bending_reference(mesh['segments'], cfg['t_mm'], axis)
            info = dict(stage=5, job_name=name, source_case=case, input_sha256=sha256(path),
                thickness_mm=cfg['t_mm'], bending_axis_deg=axis, pm_angle_deg=angle,
                load_case=pattern['definition'], reference_axial_force_N=pattern['axial_force_N'],
                reference_moment_Nmm=pattern['moment_Nmm'], end_area_mm2=mesh['area_mm2'],
                end_area_weights=area_weights, end_rotation_weights=rotation,
                section_for_PM=dict(area_mm2=mesh['area_mm2'], squash_load_N=mesh['area_mm2']*args.fy,
                    first_yield_moment_Nmm=bend['moment_Nmm']*args.fy,
                    plastic_moment_Nmm=ref['plastic_modulus_mm3']*args.fy, c_extreme_mm=ref['c_extreme_mm']),
                settings=dict(fy=args.fy, reference_stress=args.fy, field_frequency=1,
                    initial_arc=.01, min_arc=1e-8, max_arc=.05, max_increments=5000,
                    postpeak_stop_ratio=.70, stop_method='monitor', riks_extrapolation=args.riks_extrapolation),
                source_step4=dict(case=dict(label=case, **provenance)),
                hss_report_reference=cfg, modeling_scope='continuous closed S4R HSS; no weld/residual stress/corner strengthening model')
            if controls == 'robust':
                info['settings']['riks_controls'] = controls
                info['solution_controls'] = dict(analysis='bounded Newton / discontinuous',
                    equilibrium_tolerances='Abaqus defaults', stabilization=False,
                    line_search_iterations=4, divergence_check_after=8,
                    convergence_rate_check_after=10, equilibrium_iteration_limit=16)
            stable_json(sidecar, info)
            jobs.append(name)
    stable_json(directory/'hss_manifest.json', dict(geometry=cfg, axis_deg=axis, jobs=jobs,
                model_cases=args.model_cases, mesh_mm=args.mesh_mm, end_condition='both ends U1 U2 UR3=0; one middle U3 anchor; free end warping'))
    return jobs


def save_cae(args, directory, files, destination):
    if destination.is_file():
        if destination.stat().st_size == 0:
            raise ValueError('Existing HSS CAE is empty; preserved without overwrite: '+str(destination))
        return
    log_path = Path(directory)/'HSS_CAE_save.log'
    command(args, [args.abaqus_cmd, 'cae', 'noGUI='+str(SCRIPT_DIR/'abaqus_hss_nogui.py'), '--',
                   '--native-save-cae', str(destination), '--native-inputs']+[str(path) for path in files],
            directory, log_path=log_path)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError('Abaqus did not preserve a nonempty HSS CAE: '+str(destination)+'\n'+
                           native_diagnostics(directory, log_path))


def native_save(argv):
    """Abaqus/CAE-only adapter; solver input remains the verified original INP."""
    from abaqus import mdb
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--native-save-cae', required=True)
    parser.add_argument('--native-inputs', nargs='+', required=True)
    args = parser.parse_args(user_arguments(argv))
    for path in args.native_inputs:
        path = Path(path)
        model = mdb.ModelFromInputFile(name=path.stem, inputFileName=str(path))
        sidecar = path.with_name(path.stem+'_step5.json')
        if sidecar.is_file():
            from abaqus_step5_force_displacement_core import STEP5_PREFIX
            model.setValues(description=STEP5_PREFIX+sidecar.read_text(encoding='utf-8'))
        mdb.Job(name=path.stem, model=path.stem)
    mdb.saveAs(pathName=str(Path(args.native_save_cae).with_suffix('')))


def run(args):
    plan = preflight(args)
    if args.dry_run:
        print(json.dumps(plan, indent=2, allow_nan=False)); return plan
    cfg = plan['geometry']; mesh = make_mesh(cfg, args.mesh_mm)
    root = Path(args.output_root).expanduser().resolve()
    if not args.collect_only:
        root.mkdir(parents=True, exist_ok=True)
        # Stable provenance prevents --resume silently accepting a changed model.
        preserve_pipeline_plan(root/'hss_pipeline.json', pipeline_request(args, plan))
        for entry in plan['pm_dirs']:
            directory = Path(entry['pm_dir']); axis_dir = directory.parent
            axis_dir.mkdir(parents=True, exist_ok=True)
            modes, evidence = (None, None)
            if args.model_cases != 'perfect':
                shared_reference = root/'reference'
                shared_reference.mkdir(parents=True, exist_ok=True)
                modes, evidence = own_modes(args, shared_reference, mesh, cfg, entry['axis_deg'])
                source = shared_reference/'HSS_reference'
                save_cae(args, source, [source/'HSS_reference.inp'], source/'HSS_reference.cae')
            jobs = prepare_points(args, directory, mesh, cfg, entry['axis_deg'], modes, evidence)
            save_cae(args, directory, [directory/(name+'.inp') for name in jobs], directory/'HSS_PM.cae')
            queue = [args.abaqus_cmd, 'cae', 'noGUI='+str(SCRIPT_DIR/'abaqus_step5_queue_nogui.py'), '--',
                     '--run-dir', str(directory), '--jobs']+jobs+['--cpus', str(args.cpus),
                     '--parallel-jobs', str(args.parallel_jobs), '--stop-method', 'monitor',
                     '--abaqus-cmd', args.abaqus_cmd]
            if args.resume:
                queue += ['--resume']
            command(args, queue, directory)
    for entry in plan['pm_dirs']:
        directory = Path(entry['pm_dir'])
        if not (directory/'STEP5_PM_points.csv').is_file():
            raise ValueError('HSS P-M points do not exist: '+str(directory))
    return plan


def main(argv=None):
    argv = user_arguments(argv)
    if '--native-save-cae' in argv:
        return native_save(argv)
    return run(parse_arguments(argv))


if __name__ == '__main__':
    main()
