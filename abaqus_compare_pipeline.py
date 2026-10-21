"""Run the independent no-plate/plate family pipelines and compare their P-M points.

Run with normal Python. Each existing repository runs in its own child process,
with its own imports, geometry builder and resume checks. Dataset pipelines run
sequentially; --parallel-jobs controls independent nonlinear solves within each
queue, sharing the full CPU/RAM budget. This module builds no FE model.
"""
import argparse
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
PLAN_PREFIX = 'COMPARE_PLAN_JSON:'
# This probe only parses/checks existing inputs. No build, solve, export or plot.
PLAN_PROBE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import abaqus_family_pipeline as pipeline
args = pipeline.parse_arguments(json.loads(sys.argv[2]))
configs = pipeline.skip_configurations(args) if hasattr(pipeline, 'skip_configurations') else [args]
outputs = []
for config in configs:
    for family, run, state in pipeline.preflight(config):
        outputs.append(dict(family=family.name,
                            pm_dir=str(run/'imperfection'/pipeline.pm_directory(config)),
                            skip_count=getattr(config, 'steel_plate_skip_count', None)))
print('COMPARE_PLAN_JSON:'+json.dumps(outputs))
'''
HSS_PLAN_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import abaqus_hss_pipeline as pipeline
args = pipeline.parse_arguments(json.loads(sys.argv[2]))
print('COMPARE_PLAN_JSON:'+json.dumps(pipeline.preflight(args)['pm_dirs']))
'''


def parse_arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--family-root', required=True, type=Path)
    p.add_argument('--output-root', required=True, type=Path,
                   help='Common parent: no_plates/, with_plates/, diagram/')
    p.add_argument('--no-plates-repo', type=Path, default=HERE.parent/'Abaqus_run')
    p.add_argument('--plates-repo', '--repo', dest='plates_repo', type=Path, default=HERE)
    p.add_argument('--no-plates-output-root', type=Path, help='Optional existing independent output root')
    p.add_argument('--plates-output-root', type=Path, help='Optional existing independent output root')
    p.add_argument('--variants', choices=('both','no-plates','plates'), default='both')
    loads = p.add_mutually_exclusive_group()
    loads.add_argument('--load-case', action='append', choices=('compression','bending'),
                       help='May be repeated to request both cases explicitly')
    loads.add_argument('--load-cases', help='Comma-separated compression,bending; default bending')
    p.add_argument('--bending-axis-deg', type=float, default=0.)
    p.add_argument('--pm-interaction', action='store_true', default=True,
                   help='P-M interaction is always enabled in this comparison workflow')
    p.add_argument('--pm-only', action='store_true',
                   help='Prepare required reference/Step4 inputs and solve P-M only; skip standalone Step5')
    p.add_argument('--parallel-jobs', default='1',
                   help='Independent nonlinear jobs per queue: positive integer or auto (2); CPUs/RAM are shared')
    p.add_argument('--pm-axes', default='required')
    p.add_argument('--pm-angles', default='0,15,30,45,60,75,90')
    p.add_argument('--model-cases', choices=('both','perfect','imperfect'), default='both')
    p.add_argument('--pm-directory', help='Optional common P-M directory name')
    p.add_argument('--no-plates-pm-directory', help='Override the no-plate P-M directory name')
    p.add_argument('--plates-pm-directory', help='Override the plated P-M directory name')
    p.add_argument('--step5-directory', help='Optional common primary Step-5 directory name')
    p.add_argument('--cufsm-report-dir', type=Path, help='Optional legacy report; default: compute from family geometry')
    p.add_argument('--hss-radius-mm', type=float, help='Optional HSS centre-line corner radius override')
    p.add_argument('--demand-csv', type=Path, help='Display-only building demand points; no orientation or design verdict inferred')
    p.add_argument('--include-hss', action='store_true',
                   help='Build/solve the report box_same_A closed HSS and add its curves to the common diagram')
    p.add_argument('--hss-model-cases', choices=('both','perfect','imperfect'), default='both',
                   help='HSS cases, using its own reference modes for imperfections; default both')
    p.add_argument('--abaqus-cmd', default='abaqus')
    p.add_argument('--cpus', default='auto')
    p.add_argument('--mesh-mm', type=float, default=20.)
    p.add_argument('--n-modes', type=int, default=250)
    p.add_argument('--n-vectors', type=int, default=500)
    p.add_argument('--max-iterations', type=int, default=1250)
    p.add_argument('--buckle-output', choices=('standard','detailed'), default='detailed')
    p.add_argument('--nodal-precision', choices=('single','full'), default='full')
    p.add_argument('--longitudinal-lines', type=int, default=4)
    p.add_argument('--longitudinal-line-min-spacing-mm', type=float, default=5.)
    p.add_argument('--steel-column-plate-fy', type=float, default=240.)
    p.add_argument('--steel-plate-fy', type=float, default=360.)
    p.add_argument('--steel-plate-length', type=float, default=0.)
    p.add_argument('--steel-plate-length-2', type=float, default=0.)
    p.add_argument('--steel-plate-gap-mm', type=float, default=0.)
    p.add_argument('--steel-plate-skip-count', default='0')
    p.add_argument('--local-mode', type=int, default=1)
    p.add_argument('--global-modes', default='1', help='Shared REF_G mode choice for both repositories; default 1')
    p.add_argument('--riks-extrapolation', choices=('linear','none'), default='linear')
    p.add_argument('--riks-controls', choices=('auto','default','robust'), default='auto',
                   help='Auto: bounded Newton controls for new jobs; keep saved controls on resume')
    p.add_argument('--start-family')
    p.add_argument('--resume', action='store_true')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Read-only preflight and command listing')
    mode.add_argument('--collect-only', action='store_true', help='Replot existing verified inputs; never run model pipelines')
    args = p.parse_args(argv)
    if args.parallel_jobs != 'auto':
        try:
            if str(int(args.parallel_jobs)) != args.parallel_jobs or int(args.parallel_jobs)<1:
                raise ValueError
        except ValueError:p.error('--parallel-jobs must be auto or a positive integer')
    cases = (args.load_cases.split(',') if args.load_cases is not None else args.load_case) or ['bending']
    cases = [value.strip() for value in cases]
    if set(cases)-{'compression','bending'} or len(cases)!=len(set(cases)):
        p.error('Select distinct load cases: compression,bending')
    args.load_cases = cases
    if not math.isfinite(args.bending_axis_deg):
        p.error('--bending-axis-deg must be finite')
    if args.variants != 'no-plates' and (not math.isfinite(args.steel_plate_length) or args.steel_plate_length<=0):
        p.error('The plated variant requires a positive --steel-plate-length')
    for name in ('family_root','output_root','no_plates_repo','plates_repo',
                 'no_plates_output_root','plates_output_root','cufsm_report_dir'):
        value = getattr(args,name)
        if value is not None:setattr(args,name,value.expanduser().resolve())
    args.no_plates_output_root = args.no_plates_output_root or args.output_root/'no_plates'
    args.plates_output_root = args.plates_output_root or args.output_root/'with_plates'
    if args.variants == 'both':
        a,b = args.no_plates_output_root,args.plates_output_root
        if a==b or a in b.parents or b in a.parents:
            p.error('No-plate and plated analysis output roots must be separate, non-nested directories')
        if args.no_plates_repo==args.plates_repo:
            p.error('The no-plate and plated variants must use their respective independent repositories')
    if args.collect_only:args.resume=True
    return args


def build_requests(args):
    """Share numerical/material settings; forward plate geometry to its own builder only."""
    variants = ['no_plates','with_plates'] if args.variants=='both' else [
        'no_plates' if args.variants=='no-plates' else 'with_plates']
    result = []
    common = ('mesh_mm','n_modes','n_vectors','max_iterations','cpus','buckle_output','nodal_precision',
              'longitudinal_lines','longitudinal_line_min_spacing_mm','steel_column_plate_fy',
              'local_mode','model_cases','pm_axes','pm_angles','riks_extrapolation','riks_controls','abaqus_cmd','parallel_jobs')
    for case in args.load_cases:
        for variant in variants:
            repo = args.no_plates_repo if variant=='no_plates' else args.plates_repo
            output = args.no_plates_output_root if variant=='no_plates' else args.plates_output_root
            command = ['--family-root',str(args.family_root),'--output-root',str(output),
                       '--repo',str(repo),'--load-case',case,'--pm-interaction']
            for name in common:
                command.extend(['--'+name.replace('_','-'),str(getattr(args,name))])
            if case=='bending':command.extend(['--bending-axis-deg',str(args.bending_axis_deg)])
            for name in ('global_modes','cufsm_report_dir','step5_directory','start_family'):
                value=getattr(args,name)
                if value is not None:command.extend(['--'+name.replace('_','-'),str(value)])
            pm_dir = (args.no_plates_pm_directory if variant=='no_plates' else args.plates_pm_directory) or args.pm_directory
            if pm_dir:command.extend(['--pm-directory',pm_dir])
            if variant=='with_plates':
                for name in ('steel_plate_fy','steel_plate_length','steel_plate_length_2',
                             'steel_plate_gap_mm','steel_plate_skip_count'):
                    command.extend(['--'+name.replace('_','-'),str(getattr(args,name))])
            if args.resume:command.append('--resume')
            if args.pm_only:command.append('--pm-only')
            result.append(dict(variant=variant,load_case=case,repo=repo,
                               output_root=output,arguments=command))
    if args.include_hss:
        hss_output=args.output_root/'hss'
        command=['--family-root',str(args.family_root),
                 '--output-root',str(hss_output),'--axes',args.pm_axes,'--pm-angles',args.pm_angles,
                 '--model-cases',args.hss_model_cases,'--fy',str(args.steel_column_plate_fy)]
        for name in ('mesh_mm','n_modes','n_vectors','max_iterations','cpus','parallel_jobs',
                     'local_mode','global_modes','abaqus_cmd','riks_extrapolation','riks_controls'):
            command.extend(['--'+name.replace('_','-'),str(getattr(args,name))])
        if args.cufsm_report_dir:command.extend(['--cufsm-report-dir',str(args.cufsm_report_dir)])
        if args.hss_radius_mm is not None:command.extend(['--hss-radius-mm',str(args.hss_radius_mm)])
        if args.pm_directory: command.extend(['--pm-directory', args.pm_directory])
        if args.resume:command.append('--resume')
        if args.collect_only:command.append('--collect-only')
        result.append(dict(variant='hss',load_case='P-M',repo=HERE,script='abaqus_hss_pipeline.py',
                           output_root=hss_output,arguments=command))
    return result


def child_environment(repo):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    # Each repository takes precedence; no imported modules cross process boundaries.
    environment['PYTHONPATH'] = os.pathsep.join([str(repo),str(repo/'gdlc_classifier'),
                                              environment.get('PYTHONPATH','')])
    return environment


def probe_request(request):
    script = request['repo']/request.get('script','abaqus_family_pipeline.py')
    if not script.is_file():raise ValueError('Missing independent pipeline: '+str(script))
    probe=HSS_PLAN_PROBE if request['variant']=='hss' else PLAN_PROBE
    command = [sys.executable,'-B','-c',probe,str(request['repo']),json.dumps(request['arguments'])]
    process = subprocess.run(command,cwd=str(request['repo']),env=child_environment(request['repo']),
                             text=True,encoding='utf-8',errors='replace',capture_output=True)
    if process.returncode:
        raise ValueError('Preflight failed for %s/%s:\n%s\n%s' %
                         (request['variant'],request['load_case'],process.stdout,process.stderr))
    records = [line[len(PLAN_PREFIX):] for line in process.stdout.splitlines() if line.startswith(PLAN_PREFIX)]
    if len(records)!=1:raise ValueError('Independent pipeline did not return a unique comparison plan')
    plans = json.loads(records[0])
    if not plans:raise ValueError('No families selected for comparison')
    return plans


def run_comparison(args,probe=probe_request,runner=subprocess.run):
    demand_path = getattr(args, 'demand_csv', None)
    if demand_path:
        from abaqus_pm_demands import read_demands
        demand_path = demand_path.expanduser().resolve()
        read_demands(demand_path)  # Validate before any solver or output write.
    try:
        plotting=importlib.import_module('matplotlib')
        plotting.use('Agg')
        importlib.import_module('matplotlib.pyplot')
    except ImportError as error:
        raise RuntimeError('Matplotlib and its plotting dependencies must import successfully '
                           'in this Python interpreter before starting analyses') from error
    requests = build_requests(args)
    outputs,labels = [],[]
    # Preflight every independent request before any output write or real solver.
    for request in requests:
        request['plans'] = probe(request)
        for plan in request['plans']:
            output = Path(plan['pm_dir']).resolve()
            if request['output_root'] not in output.parents:
                raise ValueError('P-M destination outside its independent output root: '+str(output))
            if output in outputs:raise ValueError('Duplicate P-M comparison destination: '+str(output))
            outputs.append(output)
            if request['variant']=='hss':
                label=plan.get('label','HSS')+' | axis '+str(plan.get('axis_deg','all'))
            else:
                label = ('No plates' if request['variant']=='no_plates' else 'With plates')
                label += ' | '+plan['family']+' | '+request['load_case']
            if plan.get('skip_count') is not None:label += ' | skip '+str(plan['skip_count'])
            labels.append(label)
    if not outputs:raise ValueError('No P-M comparison datasets planned')
    commands = [[sys.executable,'-B',str(request['repo']/request.get('script','abaqus_family_pipeline.py')),*request['arguments']]
                for request in requests]
    collector = [sys.executable,'-B',str(HERE/'abaqus_pm_interaction.py'),'--mode','collect',
                 '--roots',*[str(path) for path in outputs],'--dataset-labels',*labels,
                 '--output-dir',str(args.output_root/'diagram'),'--fy',str(args.steel_column_plate_fy)]
    if args.cufsm_report_dir:collector.extend(['--cufsm-report-dir',str(args.cufsm_report_dir)])
    if demand_path:collector.extend(['--demand-csv',str(demand_path)])
    if args.dry_run:
        print('READ-ONLY COMPARISON PLAN (no outputs created):',flush=True)
        for command in ([] if args.collect_only else commands)+[collector]:
            print(subprocess.list2cmdline(command),flush=True)
        return
    manifest_path = args.output_root/'comparison_manifest.json'
    if manifest_path.exists() and not args.resume:
        raise ValueError('Comparison already exists; use --resume or a fresh --output-root')
    if args.collect_only:
        for path in outputs:
            if not (path/'STEP5_PM_points.csv').is_file():
                raise ValueError('--collect-only requires existing P-M points: '+str(path))
    args.output_root.mkdir(parents=True,exist_ok=True)
    manifest = dict(schema='independent_pm_comparison_v1',status='RUNNING',
                    family_root=str(args.family_root),fy_MPa=args.steel_column_plate_fy,
                    model_cases=args.model_cases,load_cases=args.load_cases,
                    pm_only=args.pm_only,parallel_jobs=args.parallel_jobs,
                    include_hss=args.include_hss,hss_model_cases=args.hss_model_cases,
                    analysis_commands=commands,collector_command=collector,
                    datasets=[dict(root=str(path),label=label) for path,label in zip(outputs,labels)])
    def save():manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    save()
    try:
        if not args.collect_only:
            live_context = json.dumps(dict(output_dir=str(args.output_root/'diagram'),
                datasets=[dict(root=str(path), label=label) for path,label in zip(outputs,labels)]))
            for request,command in zip(requests,commands):
                print('\nCOMPARE: '+request['variant']+' / '+request['load_case'],flush=True)
                environment = child_environment(request['repo'])
                environment['STEP5_LIVE_REPORT_CONTEXT'] = live_context
                runner(command,cwd=str(request['repo']),env=environment,check=True)
        for path in outputs:
            if not (path/'STEP5_PM_points.csv').is_file():
                raise RuntimeError('Planned P-M points were not produced: '+str(path))
        runner(collector,cwd=str(HERE),env=child_environment(HERE),check=True)
        for name in ('PM_interaction_points.csv','PM_interaction.png','PM_interaction.svg'):
            if not (args.output_root/'diagram'/name).is_file():
                raise RuntimeError('Combined comparison output missing: '+name)
    except BaseException as error:
        manifest.update(status='FAILED',error=str(error));save()
        raise
    audit_path=args.output_root/'diagram'/'PM_analysis_audit.json'
    if audit_path.is_file():
        manifest['analysis_summary']=json.loads(audit_path.read_text(encoding='utf-8'))['summary']
        summary=manifest['analysis_summary']
        if summary['solver_errors'] or summary['path_reviews'] or summary['omitted_points']:
            print('COMPARISON REVIEW: processing completed; some capacities remain unverified. See '+str(audit_path),flush=True)
    manifest.update(status='COMPLETED');save()
    print('\nCOMBINED P-M: '+str(args.output_root/'diagram'/'PM_interaction.png'),flush=True)


def main(argv=None):
    args = parse_arguments(argv)
    try:run_comparison(args)
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:
        print('COMPARISON FAILED: '+str(error),file=sys.stderr,flush=True)
        return 1
    except KeyboardInterrupt:
        print('COMPARISON INTERRUPTED; inspect running jobs before --resume.',file=sys.stderr)
        return 130
    return 0


if __name__=='__main__':
    sys.exit(main())
