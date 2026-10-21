"""P-M interaction generator: ray loads, INP variants, orchestration with fake CAE objects, collection.
No Abaqus execution."""
import csv
import json
import math
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import abaqus_load_case as load_cases
import abaqus_pm_interaction as pm
import abaqus_step5_gmnia as step5
from test_step5_gmnia import _fake_bending_mesh


def fake_template():
    instances, segments, unused = _fake_bending_mesh()
    end_meshes = [(name, dict((n.label, n.coordinates) for n in inst.nodes),
                   [tuple(n.label for n in e.getNodes()) for e in inst.elements])
                  for name, inst in sorted(instances.items())]
    return step5.pm_template_nodes(end_meshes, 0., 100.), segments


def cae_like_inp(template):
    """Text in the layout Abaqus/CAE writes for named concentrated forces in a Riks step."""
    lines = ['*Heading', '** Job name: PMT_X Model name: PMT_X', '*Element, type=CONN3D2, elset=W',
             '*Connector Section, elset=W', 'Beam,', '*Plastic', '240., 0.',
             '*Step, name=GMNIA, nlgeom=YES', '*Static, riks', '0.01, 1., 1e-08, 0.05', '**',
             '** LOADS', '**']
    for node in template['nodes']:
        lines += ['** Name: %s   Type: Concentrated force' % node['load'], '*Cload', '%s, 3, 1.' % node['set']]
    lines += ['*Output, field', '*Element Output', 'S, LE, PEEQ', '*Element Output, elset=W', 'CTF1, CTF2',
              '** STEP5 URDFIL trigger', '*NODE FILE, NSET=STEP5_STOP, FREQUENCY=1', 'U', '*End Step', '']
    return '\n'.join(lines)


def fake_end_plates():
    """End-plate records in the layout of step5.create_end_plates (no CAE)."""
    return [dict(side=side, set='STEP5_RP_'+suffix, xyz=[0., 0., z], coupling='STEP5_END_PLATE_'+suffix,
                 force_load='STEP5_END_%s_FORCE' % suffix, moment_load='STEP5_END_%s_MOMENT' % suffix)
            for side, suffix, z in ((0, 'BOTTOM', 0.), (1, 'TOP', 100.))]


def cae_like_gmnia_inp():
    """CAE layout of the GMNIA template: placeholder CF3 and CM1/CM2 at the end-plate RPs."""
    lines = ['*Heading', '** Job name: PMT_X Model name: PMT_X', '*Element, type=CONN3D2, elset=W',
             '*Connector Section, elset=W', 'Beam,', '*Plastic', '240., 0.']
    for plate in fake_end_plates():
        lines += ['** Constraint: %s' % plate['coupling'],
                  '*Coupling, constraint name=%s, ref node=%s, surface=%s_CNS_, influence radius=1e+20'
                  % (plate['coupling'], plate['set'], plate['coupling']), '*Kinematic', '1, 3']
    lines += ['*Step, name=GMNIA, nlgeom=YES', '*Static, riks', '0.01, 1., 1e-08, 0.05', '**',
              '** LOADS', '**']
    for plate in fake_end_plates():
        lines += ['** Name: %s   Type: Concentrated force' % plate['force_load'], '*Cload',
                  '%s, 3, 1.' % plate['set'],
                  '** Name: %s   Type: Moment' % plate['moment_load'], '*Cload',
                  '%s, 4, 1.' % plate['set'], '%s, 5, 1.' % plate['set']]
    lines += ['*Output, field', '*Element Output', 'S, LE, PEEQ', '*Element Output, elset=W', 'CTF1, CTF2',
              '*Contact Controls, stiffness scale factor=USER ADAPTIVE', '0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.7, 1.',
              '*Controls, analysis=DISCONTINUOUS', '*Controls, parameters=LINE SEARCH', '5',
              '** STEP5 URDFIL trigger', '*NODE FILE, NSET=STEP5_STOP, FREQUENCY=1', 'U', '*End Step', '']
    return '\n'.join(lines)


def cload_dof_values(text):
    """{load name: {dof: value}} of every named *Cload block."""
    out, name = {}, None
    for line in text.split('\n'):
        if line.startswith('** Name:'):
            name = line.split()[2]
        elif name and line[:1].isalnum():
            region, dof, value = [f.strip() for f in line.split(',')]
            out.setdefault(name, {})[int(dof)] = float(value)
        elif line.startswith('*') and not line.upper().startswith('*CLOAD'):
            name = None
    return out


def cload_values(text):
    values, name = {}, None
    for line in text.split('\n'):
        if line.startswith('** Name:'):
            name = line.split()[2]
        elif name and not line.startswith('*'):
            values[name] = float(line.split(',')[2])
            name = None
    return values


class PMInteractionTests(unittest.TestCase):
    def _case_plot_lines(self, points, normalized=False):
        import matplotlib.pyplot as plt
        from matplotlib.figure import Figure
        captured=[];original=plt.subplots
        def capture(*args,**kwargs):
            fig,axes=original(*args,**kwargs)
            captured.extend(list(axes.flat) if hasattr(axes,'flat') else [axes])
            return fig,axes
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory, \
                mock.patch.object(plt,'subplots',side_effect=capture), \
                mock.patch.object(Figure,'savefig'):
            pm.plot_interaction(directory,points,{},normalized=normalized)
        return [[line for line in ax.lines if line.get_label().startswith('GMNIA')] for ax in captured]

    def test_provisional_case_markers_stay_distinct_on_dimensional_normalized_and_panel_plots(self):
        points=[]
        for case in ('PERFECT','LG_mp'):
            for axis in (0.,90.):
                for i,psi in enumerate((30.,60.)):
                    points.append(dict(source='GMNIA',source_case=case,load_case='combined',
                        dataset=os.path.abspath('specimen'),bending_axis_deg=axis,pm_angle_deg=psi,
                        M_kNm=1.+i,P_kN=3.-i,M_over_My=.1*(1.+i),P_over_Py=.1*(3.-i),
                        selection_kind='INCOMPLETE_ASCENDING_ENDPOINT',capacity_verified=False))
        for normalized in (False,True):
            panels=self._case_plot_lines(points,normalized)
            self.assertEqual(len(panels),4)  # two separate figures and the two-panel figure
            for lines in panels:
                self.assertEqual(len(lines),2)
                by_case={line.get_label().split()[1]:line for line in lines}
                perfect,imperfect=by_case['PERFECT'],by_case['LG_mp']
                self.assertEqual(perfect.get_marker(),'o')
                self.assertEqual(imperfect.get_marker(),'s')
                self.assertNotEqual(perfect.get_color(),imperfect.get_color())
                for line in lines:
                    self.assertEqual(line.get_linestyle(),'-' if line is perfect else '--')
                    self.assertEqual(line.get_markerfacecolor(),'white')
                    self.assertIn('incomplete/provisional',line.get_label())
                    self.assertEqual(len(line.get_xdata()),2)

    def test_verified_case_curves_use_case_styles_and_keep_imperfection_cases_separate(self):
        points=[]
        for case in ('PERFECT','STEP4_LG_mp','STEP4_LDG_pp'):
            for i,psi in enumerate((30.,60.)):
                points.append(dict(source='GMNIA',source_case=case,load_case='combined',
                    bending_axis_deg=0.,pm_angle_deg=psi,M_kNm=1.+i,P_kN=3.-i,
                    selection_kind='VERIFIED_PEAK',capacity_verified=True))
        lines=self._case_plot_lines(points)[0]
        self.assertEqual(len(lines),3)
        by_case={line.get_label().split()[1]:line for line in lines}
        self.assertEqual(set(by_case),{'PERFECT','LG_mp','LDG_pp'})
        perfect=by_case['PERFECT']
        self.assertEqual((perfect.get_marker(),perfect.get_linestyle()),('o','-'))
        for case in ('LG_mp','LDG_pp'):
            line=by_case[case]
            self.assertEqual((line.get_marker(),line.get_linestyle()),('s','--'))
            self.assertNotEqual(line.get_color(),perfect.get_color())
            self.assertEqual(len(line.get_xdata()),2)

    def test_case_identity_falls_back_to_legacy_model_and_job_names(self):
        fixtures=[(dict(model='PM_A000_P30_PERFECT'),'PERFECT','o'),
                  (dict(model='PM_P00_LG_mp'),'LG_mp','s'),
                  (dict(model='STEP5_LDG_pp_FY240'),'LDG_pp','s'),
                  (dict(job_name='STEP5_PERFECT_FY240'),'PERFECT','o'),
                  (dict(job='STEP4_custom_shape'),'custom_shape','s'),
                  (dict(source_case='STEP4_custom_shape',model='PM_P00_PERFECT'),'custom_shape','s'),
                  (dict(source_case='IMPERFECT'),'IMPERFECT','s')]
        for metadata,case,marker in fixtures:
            with self.subTest(metadata=metadata):
                point=dict(source='GMNIA',load_case='combined',bending_axis_deg=0.,pm_angle_deg=30.,
                           M_kNm=1.,P_kN=2.,selection_kind='VERIFIED_PEAK',capacity_verified=True,**metadata)
                line=self._case_plot_lines([point])[0][0]
                self.assertEqual(line.get_label().split()[1],case)
                self.assertEqual(line.get_marker(),marker)

    def test_case_curve_connects_across_selection_status_without_changing_points(self):
        import copy
        points=[]
        selections=[('VERIFIED_PEAK',True),('SIGNIFICANT_STIFFNESS_CHANGE',False),
                    ('SIGNIFICANT_LOAD_DROP',False),('INCOMPLETE_ASCENDING_ENDPOINT',False),
                    ('VERIFIED_PEAK',False)]
        for group,(kind,verified) in enumerate(selections):
            for i,psi in enumerate((30.,60.)):
                points.append(dict(source='GMNIA',source_case='PERFECT',load_case='combined',
                    bending_axis_deg=0.,pm_angle_deg=psi,M_kNm=10.*group+i+1.,P_kN=3.-i,
                    selection_kind=kind,capacity_verified=verified,provisional=not verified,
                    selection_policy='response-v1',frame=5+group,lpf=.5,
                    selection_diagnostics={'evidence':'retained'}))
        original=copy.deepcopy(points)
        lines=self._case_plot_lines(points)[0]
        self.assertEqual(points,original)
        self.assertEqual(len(lines),6)
        connected=[line for line in lines if line.get_linestyle()!='None']
        self.assertEqual(len(connected),1)
        self.assertEqual(set(connected[0].get_xdata()),{float(10*g+i+1) for g in range(5) for i in range(2)})
        self.assertEqual(connected[0].get_linestyle(),'-')
        for line in lines:
            if line not in connected:
                self.assertEqual(line.get_marker(),'o')
                self.assertEqual(len(line.get_xdata()),2)
        self.assertEqual(sum(line.get_markerfacecolor()=='white' for line in lines if line not in connected),4)

    def test_endpoint_only_connection_is_labeled_and_does_not_claim_sampled_envelope(self):
        points=[dict(source='GMNIA',source_case='PERFECT',load_case='combined',
                     bending_axis_deg=0.,pm_angle_deg=psi,M_kNm=m,P_kN=p,
                     selection_kind='INCOMPLETE_ASCENDING_ENDPOINT',capacity_verified=False)
                for psi,m,p in [(0.,0.,10.),(90.,2.,0.)]]
        line=self._case_plot_lines(points)[0][0]
        self.assertEqual(line.get_linestyle(),'-')
        self.assertIn('endpoint-only',line.get_label())
        self.assertIn('envelope not sampled',line.get_label())
        self.assertEqual(line.get_markerfacecolor(),'white')

    def test_multiple_dataset_case_styles_are_distinct_and_order_independent(self):
        points=[]
        for dataset in ('plain','plated'):
            for case in ('PERFECT','LG_mp'):
                for i,psi in enumerate((30.,60.)):
                    points.append(dict(source='GMNIA',source_case=case,dataset=os.path.abspath(dataset),
                        dataset_label=dataset,load_case='combined',bending_axis_deg=0.,pm_angle_deg=psi,
                        M_kNm=1.+i,P_kN=3.-i,selection_kind='VERIFIED_PEAK',capacity_verified=True))
        def styles(rows):
            return {line.get_label():(line.get_color(),line.get_marker(),line.get_linestyle())
                    for line in self._case_plot_lines(rows)[0]}
        first=styles(points)
        self.assertEqual(len(first),4)
        self.assertEqual(len(set(first.values())),4)
        self.assertEqual(first,styles(list(reversed(points))))
        for label,(color,marker,linestyle) in first.items():
            self.assertEqual((marker,linestyle),('o','-') if 'PERFECT' in label else ('s','--'))
        for case in ('PERFECT','LG_mp'):
            self.assertEqual(len({style[0] for label,style in first.items() if case in label}),2)

    def test_collect_dataset_labels_align_with_roots_and_preserve_dataset_identity_and_sources(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            roots=[Path(directory)/name/'Step5_PM' for name in ('plain','plated')]
            original={}
            for root in roots:
                root.mkdir(parents=True)
                path=root/'STEP5_PM_points.csv'
                path.write_text('model,source_case,load_case,bending_axis_deg,pm_angle_deg,P_kN,M_kNm,outcome\n'
                                'PM_A000_P30_PERFECT,PERFECT,combined,0,30,12,2,FAILED\n')
                original[path]=path.read_bytes()
            argv=['--mode','collect','--roots']+list(map(str,roots))+['--dataset-labels','No plates','With plates',
                 '--output-dir',str(Path(directory)/'comparison')]
            with mock.patch('abaqus_pm_demands.write_readable_plots',return_value=[]) as plot:
                result=pm.collect(pm.parse_arguments(argv))
            with open(result['csv'],encoding='utf-8-sig') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual([row['dataset_label'] for row in rows],['No plates','With plates'])
            self.assertEqual({row['dataset'] for row in rows},{str(root.resolve()) for root in roots})
            self.assertEqual([row['P_kN'] for row in rows],['12','12'])
            self.assertEqual([row['M_kNm'] for row in rows],['2','2'])
            self.assertEqual([row['dataset_label'] for row in plot.call_args.args[1]],['No plates','With plates'])
            for path,data in original.items():self.assertEqual(path.read_bytes(),data)

    def test_dataset_labels_require_collect_and_one_unique_nonempty_label_per_root(self):
        invalid=[['--mode','collect','--roots','a','b','--dataset-labels','plain'],
                 ['--mode','collect','--roots','a','b','--dataset-labels','same','same'],
                 ['--mode','collect','--roots','a','--dataset-labels',' '],
                 ['--mode','section','--run-dir','a','--dataset-labels','plain']]
        for argv in invalid:
            with self.subTest(argv=argv),self.assertRaises(SystemExit):
                pm.parse_arguments(argv+['--output-dir','out'])

    def test_connections_never_cross_source_case_dataset_or_axis_even_with_shared_display_labels(self):
        import matplotlib.pyplot as plt
        from matplotlib.figure import Figure
        points=[]
        for index,(source,case,dataset,axis) in enumerate([
                ('GMNIA','PERFECT','plain',0.),('GMNIA','LG_mp','plain',0.),
                ('ELASTIC_BUCKLING','PERFECT','plain',0.),('GMNIA','PERFECT','plated',0.),
                ('GMNIA','PERFECT','plain',90.)]):
            for i,(psi,kind) in enumerate([(30.,'VERIFIED_PEAK'),(60.,'INCOMPLETE_ASCENDING_ENDPOINT')]):
                points.append(dict(source=source,source_case=case,dataset=os.path.abspath(dataset),
                    dataset_label='Shared label',load_case='combined',bending_axis_deg=axis,pm_angle_deg=psi,
                    M_kNm=index*10.+i+1.,P_kN=3.-i,selection_kind=kind,capacity_verified=kind=='VERIFIED_PEAK'))
        panels=[];original=plt.subplots
        def capture(*args,**kwargs):
            fig,axes=original(*args,**kwargs)
            panels.extend(list(axes.flat) if hasattr(axes,'flat') else [axes])
            return fig,axes
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory, \
                mock.patch.object(plt,'subplots',side_effect=capture),mock.patch.object(Figure,'savefig'):
            pm.plot_interaction(directory,points,{})
        for panel,count in zip(panels,(4,1,4,1)):
            curves=[line for line in panel.lines if line.get_linestyle()!='None']
            self.assertEqual(len(curves),count)
            for line in curves:
                self.assertEqual(len(line.get_xdata()),2)
                self.assertEqual(max(line.get_xdata())-min(line.get_xdata()),1.)
                self.assertIn('Shared label;',line.get_label())

    def test_axial_only_results_show_all_reference_axes_and_state_missing_member_envelope(self):
        import matplotlib.pyplot as plt
        from pathlib import Path
        square=[(-10.,-10.,10.,-10.),(10.,-10.,10.,10.),
                (10.,10.,-10.,10.),(-10.,10.,-10.,-10.)]
        reference=pm.section_reference(square,1.,[0.,90.],fy=240.,points=11)
        sections={axis:dict(data,fy_MPa=240.) for axis,data in reference['axes'].items()}
        captured=[];original=plt.subplots
        def capture(*args,**kwargs):
            fig,axes=original(*args,**kwargs);captured.extend(list(axes.flat) if hasattr(axes,'flat') else [axes])
            return fig,axes
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            points=[dict(source='GMNIA',source_case='LG_mp',load_case='combined',
                folder=directory,bending_axis_deg=None,pm_angle_deg=0.,M_kNm=0.,P_kN=10.,
                selection_kind='VERIFIED_PEAK',capacity_verified=True)]
            with mock.patch.object(plt,'subplots',side_effect=capture):
                files=pm.plot_interaction(directory,points,sections)
            self.assertTrue((Path(directory)/'PM_interaction_A090.svg').is_file())
            self.assertTrue(any(Path(path).name=='PM_interaction.png' for path in files))
            for ax in captured:
                self.assertTrue(any('not sampled' in text.get_text() for text in ax.texts))
                self.assertTrue({'A','B'}.issubset({text.get_text() for text in ax.texts}))
                gmnia=[line for line in ax.lines if line.get_label().startswith('GMNIA')]
                self.assertEqual(len(gmnia),1)
                self.assertEqual(gmnia[0].get_linestyle(),'None')
                self.assertLess(ax.get_legend().get_bbox_to_anchor().y1,ax.get_window_extent().y0)

    def test_explicit_collect_report_uses_current_model_fy_or_requires_it(self):
        import abaqus_report_reference as reader
        with tempfile.TemporaryDirectory() as root:
            model = 'STEP5_B_FY360'
            path = os.path.join(root, 'STEP5_PM_points.csv')
            with open(path, 'w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['model', 'load_case', 'bending_axis_deg', 'P_kN', 'M_kNm'])
                writer.writeheader()
                writer.writerow(dict(model=model, load_case='bending', bending_axis_deg=0., P_kN=0., M_kNm=50.))
            metadata_path = os.path.join(root, model+'_step5.json')
            with open(metadata_path, 'w') as stream:
                json.dump(dict(stage=5, settings=dict(fy=360.)), stream)
            args = pm.parse_arguments(['--mode', 'collect', '--roots', root, '--output-dir',
                                       os.path.join(root, 'diagram'), '--cufsm-report-dir', root])
            with mock.patch.object(reader, 'read_reference', return_value={'fy_MPa':360.}) as read, \
                    mock.patch('abaqus_pm_demands.write_readable_plots', return_value=[]):
                pm.collect(args)
            read.assert_called_once_with(root, fy=360.)
            with open(metadata_path, 'w') as stream:
                json.dump(dict(stage=5), stream)
            with mock.patch.object(reader, 'read_reference') as read, self.assertRaisesRegex(ValueError, '--fy'):
                pm.collect(args)
            read.assert_not_called()
            with open(metadata_path, 'w') as stream:
                json.dump(dict(stage=5, settings=dict(fy=360.)), stream)
            second = dict(source='GMNIA', folder=root, model='STEP5_OTHER')
            with open(os.path.join(root, 'STEP5_OTHER_step5.json'), 'w') as stream:
                json.dump(dict(stage=5, settings=dict(fy=240.)), stream)
            first = dict(source='GMNIA', folder=root, model=model)
            with self.assertRaisesRegex(ValueError, '--fy'):
                pm.collected_report_fy([first, second], {})

    def test_collect_uses_explicit_or_embedded_report_without_changing_capacities(self):
        import abaqus_report_reference as reader
        reference = dict(schema='cufsm_builtup_reference_v1', fy_MPa=240., AsFy_kN=1100.)
        with tempfile.TemporaryDirectory() as root:
            metadata_path = os.path.join(root, 'STEP5_B_FY240_step5.json')
            original = json.dumps(dict(stage=5, report_reference=reference))
            with open(metadata_path, 'w') as stream:
                stream.write(original)
            path = os.path.join(root, 'STEP5_PM_points.csv')
            with open(path, 'w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['model', 'load_case', 'bending_axis_deg',
                    'P_kN', 'M_kNm', 'Py_kN', 'My_kNm', 'P_over_Py', 'M_over_My'])
                writer.writeheader()
                writer.writerow(dict(model='STEP5_B_FY240', load_case='bending', bending_axis_deg=0.,
                    P_kN=0., M_kNm=50., Py_kN=1000., My_kNm=100., P_over_Py=0., M_over_My=.5))
            argv = ['--mode', 'collect', '--roots', root, '--output-dir', os.path.join(root, 'diagram')]
            with mock.patch.object(pm, 'plot_interaction', return_value=[]) as plot, \
                    mock.patch('abaqus_pm_demands.write_readable_plots',return_value=[]) as readable:
                pm.collect(pm.parse_arguments(argv))
            self.assertEqual(readable.call_args.kwargs['report_reference'], reference)
            with mock.patch.object(reader, 'read_reference', return_value=reference) as read, \
                    mock.patch.object(pm, 'plot_interaction', return_value=[]) as plot, \
                    mock.patch('abaqus_pm_demands.write_readable_plots',return_value=[]) as readable:
                args = pm.parse_arguments(argv+['--cufsm-report-dir', root, '--fy', '360'])
                result = pm.collect(args)
            read.assert_called_once_with(root, fy=360.)
            self.assertEqual(readable.call_args.kwargs['report_reference'], reference)
            with open(result['csv'], encoding='utf-8-sig') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(float(row['My_kNm']), 100.)
            self.assertEqual(float(row['M_over_My']), .5)
            with open(metadata_path) as stream:
                self.assertEqual(stream.read(), original)

    def test_normalized_plot_omits_report_lines_with_clear_reason_when_scales_missing(self):
        import contextlib, io
        import matplotlib.pyplot as plt
        reference = dict(schema='cufsm_builtup_reference_v1', AsFy_kN=1100., axes={
            'x': dict(axis_deg=0., SFy_kNm=130., ZFy_kNm=190.)})
        points = [dict(source='GMNIA', bending_axis_deg=0., M_kNm=50., P_kN=200.,
                       M_over_My=.5, P_over_Py=.2)]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(plt, 'close'), \
                contextlib.redirect_stdout(output):
            pm.plot_interaction(directory, points, {}, normalized=True, report_reference=reference)
            self.assertFalse(any('CUFSM report' in line.get_label() for line in plt.gcf().axes[0].lines))
        self.assertIn('actual panel Py/My are unavailable', output.getvalue())
        plt.close('all')

    def test_normalized_report_scales_require_actual_panel_capacity(self):
        points = [dict(bending_axis_deg=0., M_kNm=50., Py_kN=1000., My_kNm=100.),
                  dict(bending_axis_deg=90., M_kNm=40., Py_kN=1000., My_kNm=80.)]
        self.assertEqual(pm.normalized_report_scales(points, 0., None, None), (100., 1000.))
        self.assertEqual(pm.normalized_report_scales(points, 90., None, None), (80., 1000.))
        self.assertIsNone(pm.normalized_report_scales([], 0., None, None))
        points.append(dict(bending_axis_deg=0., M_kNm=50., Py_kN=1100., My_kNm=100.))
        self.assertIsNone(pm.normalized_report_scales(points, 0., None, None))
        section = dict(fy_MPa=240., first_yield_moment_Nmm_per_MPa=500000., squash_load_N_per_MPa=4000.)
        self.assertEqual(pm.normalized_report_scales([], 0., section, None), (120., 960.))

    def test_pure_compression_does_not_assign_its_unlabelled_my_to_other_axes(self):
        points = [dict(bending_axis_deg=None, M_kNm=0., P_kN=100., My_kNm=100., Py_kN=200.),
                  dict(bending_axis_deg=90., M_kNm=75., P_kN=0., My_kNm=150., Py_kN=200.)]
        self.assertEqual(pm.normalized_report_scales(points, 90., None, None), (150., 200.))

    def test_dimensional_report_benchmarks_have_correct_orientations(self):
        import matplotlib.pyplot as plt
        reference = dict(schema='cufsm_builtup_reference_v1', group='builtup_4_pieces',
                         fy_MPa=240., AsFy_kN=1100., axes={
                             'x': dict(axis_deg=0., SFy_kNm=130., ZFy_kNm=190.),
                             'z': dict(axis_deg=90., SFy_kNm=140., ZFy_kNm=200.)})
        points = [dict(source='GMNIA', bending_axis_deg=0., M_kNm=50., P_kN=200.,
                       M_over_My=.5, P_over_Py=.2, My_kNm=100., Py_kN=1000.)]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(plt, 'close'):
            pm.plot_interaction(directory, points, {}, report_reference=reference)
            ax = plt.gcf().axes[0]
            lines = {line.get_label().split()[0]: line for line in ax.lines}
            self.assertEqual(list(lines['SFy'].get_xdata()), [130., 130.])
            self.assertEqual(list(lines['ZFy'].get_xdata()), [190., 190.])
            self.assertEqual(list(lines['AsFy'].get_ydata()), [1100., 1100.])
            self.assertGreater(ax.get_xlim()[1], 190.)
            self.assertGreater(ax.get_ylim()[1], 1100.)
            pm.plot_interaction(directory, points, {}, normalized=True, report_reference=reference)
            ax = plt.gcf().axes[0]
            lines = {line.get_label().split()[0]: line for line in ax.lines}
            self.assertEqual(list(lines['SFy'].get_xdata()), [1.3, 1.3])
            self.assertEqual(list(lines['ZFy'].get_xdata()), [1.9, 1.9])
            self.assertEqual(list(lines['AsFy'].get_ydata()), [1.1, 1.1])
        plt.close('all')

    def test_combined_reference_is_unit_at_extreme_fibre_and_reduces_to_pure_cases(self):
        square = [(-100., -100., 100., -100.), (100., -100., 100., 100.),
                  (100., 100., -100., 100.), (-100., 100., -100., -100.)]
        for theta in (0., 45.):
            for psi in (0., 30., 90.):
                ref = load_cases.combined_reference(square, 2., theta, psi)
                nx, ny = ref['compression_normal']
                c = ref['c_extreme_mm']
                self.assertAlmostEqual(load_cases.unit_stress(ref, nx*c, ny*c), 1.0)
                self.assertAlmostEqual(ref['axial_weight']+ref['bending_weight'], 1.0)
        self.assertEqual(load_cases.combined_reference(square, 2., 0., 0.)['bending_weight'], 0.)
        self.assertEqual(load_cases.combined_reference(square, 2., 0., 90.)['axial_weight'], 0.)
        with self.assertRaises(ValueError):
            load_cases.combined_reference(square, 2., 0., 95.)

    def test_plastic_curve_spans_pure_bending_to_squash_load(self):
        square = [(-100., -100., 100., -100.), (100., -100., 100., 100.),
                  (100., 100., -100., 100.), (-100., 100., -100., -100.)]
        rows = load_cases.plastic_interaction(square, 2., 0., 21)
        self.assertAlmostEqual(rows[0][1], 0., places=9)
        self.assertAlmostEqual(rows[0][2], load_cases.bending_reference(square, 2., 0.)['plastic_modulus_mm3'])
        self.assertAlmostEqual(rows[-1][1], 1600.)
        self.assertAlmostEqual(rows[-1][2], 0., places=6)
        self.assertTrue(all(b[1] >= a[1]-1e-9 for a, b in zip(rows, rows[1:])))

    def test_rays_compression_once_and_names_are_valid(self):
        rays = pm.ray_list([0., 45.], [0., 30., 90.])
        self.assertEqual([r[0] for r in rays], ['PM_P00', 'PM_A000_P30', 'PM_A000_P90', 'PM_A045_P30', 'PM_A045_P90'])
        self.assertEqual(pm.check_name('PM_A022p5_P22p5_LDG_pp'), 'PM_A022p5_P22p5_LDG_pp')
        with self.assertRaises(ValueError):
            pm.check_name('PM_A022p5_P22p5_'+'X'*30)

    def test_equivalent_axes_are_rejected(self):
        unused, segments = fake_template()
        axes, info = pm.resolve_axes('required', segments)
        self.assertEqual(axes, [0., 45.])
        with self.assertRaises(ValueError):
            pm.resolve_axes([0., 90.], segments)

    def test_x_plates_keep_orthogonal_bending_axes_distinct(self):
        unused, segments = fake_template()
        symmetry = load_cases.section_symmetry(segments)
        symmetry['rotations_deg'] = [0, 180]
        symmetry['mirror_lines_deg'] = [0., 90.]
        axes, info = pm.resolve_axes([0., 90.], segments, symmetry)
        self.assertEqual(axes, [0., 90.])

    def test_ray_forces_reproduce_resultants_and_rotation_weights(self):
        template, segments = fake_template()
        bending = load_cases.bending_reference(segments, 2., 45.)
        unused, pure = pm.ray_forces(template, 2., bending)
        area = load_cases.section_properties(segments, 2.)['area_mm2']
        for psi in (0., 20., 45., 90.):
            ref = load_cases.combined_reference(segments, 2., 45., psi)
            forces, totals = pm.ray_forces(template, 2., ref)
            if psi == 0.:
                self.assertEqual(totals['moment_Nmm_per_MPa'], 0.)
            if psi == 90.:
                self.assertEqual(totals['axial_force_N_per_MPa'], 0.)
            self.assertAlmostEqual(totals['axial_force_N_per_MPa'], ref['axial_weight']*area, places=6)
            self.assertAlmostEqual(totals['moment_Nmm_per_MPa'], ref['bending_weight']*pure['moment_Nmm_per_MPa'],
                                   places=4)
            # equal and opposite at the two ends
            ends = [sum(v for n in template['nodes'] if n['side'] == s for v in [forces[n['load']]]) for s in (0, 1)]
            self.assertAlmostEqual(ends[0], -ends[1], places=6)
        weights, m_unit = pm.rotation_weights(template, 2., segments, 45.)
        nx, ny = bending['compression_normal']
        xy = dict(((n['instance'], n['label']), (n['x'], n['y'])) for n in template['nodes'] if n['side'] == 0)
        rot = sum(k*(0.3+2e-3*(nx*xy[(i, l)][0]+ny*xy[(i, l)][1])) for i, l, k in weights[0])
        self.assertAlmostEqual(rot, 2e-3, places=12)

    def test_inp_variant_replaces_only_cload_magnitudes(self):
        template, segments = fake_template()
        text = cae_like_inp(template)
        sets = dict((n['load'], (n['set'], n['instance'], n['label'])) for n in template['nodes'])
        values = dict((n['load'], 0.5*i) for i, n in enumerate(template['nodes']))
        out = pm.set_cload_values(text, values, sets)
        self.assertEqual(cload_values(out), values)
        self.assertEqual([l for l in out.split('\n') if not l[:1].isalnum() and 'P' not in l[:3]],
                         [l for l in text.split('\n') if not l[:1].isalnum() and 'P' not in l[:3]])
        # instance.label form is accepted as the same node, anything else is refused
        alt = text.replace(template['nodes'][0]['set']+', 3', '%s.%d, 3' % (template['nodes'][0]['instance'],
                                                                           template['nodes'][0]['label']), 1)
        pm.set_cload_values(alt, values, sets)
        with self.assertRaises(ValueError):
            pm.set_cload_values(text.replace(template['nodes'][0]['set']+', 3', 'OTHER, 3', 1), values, sets)
        with self.assertRaises(ValueError):
            pm.set_cload_values(text, dict(values, PM_END_MISSING=1.), dict(sets, PM_END_MISSING=('X', 'P1', 1)))

    def test_end_plate_placeholders_take_force_and_both_moment_components(self):
        text = cae_like_gmnia_inp()
        plates = fake_end_plates()
        sets = dict((p[key], (p['set'], None, None)) for p in plates for key in ('force_load', 'moment_load'))
        values = {}
        for p in plates:
            values[p['force_load']] = {3: 10.+p['side']}
            values[p['moment_load']] = {4: 2.5-p['side'], 5: -7.}
        out = pm.set_cload_values(text, values, sets)
        self.assertEqual(cload_dof_values(out), values)
        missing_dof = dict(values)
        missing_dof[plates[0]['moment_load']] = {4: 1.}
        with self.assertRaises(ValueError):            # one data line per requested DOF
            pm.set_cload_values(text, missing_dof, sets)
        with self.assertRaises(ValueError):            # the RP set is the only accepted region
            pm.set_cload_values(text.replace('STEP5_RP_TOP, 5', 'P1.1, 5'), values, sets)

    def test_gmnia_orchestration_writes_consistent_rays(self):
        template, segments = fake_template()
        with tempfile.TemporaryDirectory() as root:
            cae = os.path.join(root, 'Step4.cae')
            open(cae, 'w').close()
            out = os.path.join(root, 'out')
            models = {'JOB': NS(description='')}
            database = NS(models=models, jobs={}, close=mock.Mock())

            def prepare(model, args, pm_template=False):
                self.assertTrue(pm_template)
                return dict(stage=5, settings=dict(fy=args.fy, reference_stress=args.reference_stress,
                                                   field_frequency=1), bolts={'BOLT_S1_01': {}},
                            reference_pipeline={'nodal_precision': 'full'}, end_area_mm2=area,
                            end_area_weights=[[['P1', 1, 1.]], [['P1', 2, 1.]]],
                            source_step4=dict(case={}), end_condition=dict(step5.pipeline_contract.STEP5_END_CONDITION),
                            pm_template=json.loads(json.dumps(template)))

            def write_job(db, name, info, args, sub):
                db.jobs[name] = object()
                with open(name+'.inp', 'w') as stream:
                    stream.write(cae_like_inp(template))

            def copy(db, source, name, perfect):
                db.models[name] = object()
                return db.models[name]

            area = load_cases.section_properties(segments, 2.)['area_mm2']
            manifest = dict(reference_pipeline=dict(run_dir=root, job_name='JOB', thickness_mm=2.))
            with mock.patch.object(step5, 'open_step4_database',
                                   return_value=(database, ['PERFECT', 'STEP4_LDG_pp'], 'JOB')), \
                    mock.patch.object(step5, 'reference_step4_manifest', return_value=manifest), \
                    mock.patch.object(pm, 'reference_section', return_value=({}, {}, segments, 2.)), \
                    mock.patch.object(step5, 'copy_source_model', side_effect=copy), \
                    mock.patch.object(step5, 'prepare_model', side_effect=prepare), \
                    mock.patch.object(step5, 'write_job_input', side_effect=write_job):
                args = pm.parse_arguments(['--mode', 'gmnia', '--source-cae', cae, '--output-dir', out,
                                           '--fy', '240', '--pm-angles', '0,45,90'])
                pm.run_gmnia(args)
            files = sorted(os.listdir(out))
            jobs = sorted(f[:-4] for f in files if f.endswith('.inp'))
            self.assertEqual(len(jobs), 2*5)       # (P00 + 2 axes x 2 angles) x 2 cases
            self.assertIn('PM_A045_P45_LDG_pp', jobs)
            self.assertIn('run_pm_queue_70_pct.bat', files)
            self.assertIn(pm.MANIFEST_FILE, files)
            self.assertIn(pm.SECTION_FILE, files)
            self.assertFalse(any(f.startswith('PMT_') for f in files))
            with open(os.path.join(out, 'run_pm_queue_70_pct.bat')) as stream:
                self.assertIn('PM_A000_P90_PERFECT', stream.read())
            for job in jobs:
                with open(os.path.join(out, job+step5.METADATA_SUFFIX)) as stream:
                    info = json.load(stream)
                with open(os.path.join(out, job+'.inp')) as stream:
                    values = cload_dof_values(stream.read())
                lc = info['load_case']
                nx, ny = lc['compression_normal']
                cx, cy = lc['centroid_mm']
                self.assertEqual(set(values), {n['load'] for n in template['nodes']})
                for side, sign in ((0, 1.), (1, -1.)):
                    nodes = [n for n in template['nodes'] if n['side'] == side]
                    p = sum(values[n['load']][3] for n in nodes)
                    m = sum(values[n['load']][3]*(nx*(n['x']-cx)+ny*(n['y']-cy)) for n in nodes)
                    self.assertAlmostEqual(p, sign*info['reference_axial_force_N'], delta=1e-6*max(1., abs(p)))
                    self.assertAlmostEqual(m, sign*info['reference_moment_Nmm'], delta=1e-6*max(1., abs(m)))
                self.assertEqual(info['end_condition']['id'], step5.pipeline_contract.STEP5_END_CONDITION_ID)
                self.assertEqual(lc['type'], 'combined')
                self.assertAlmostEqual(info['section_for_PM']['squash_load_N'], area*240.)
                # LPF = 1 is first yield: P/Py + M/My = 1
                util = info['reference_axial_force_N']/info['section_for_PM']['squash_load_N'] + \
                    info['reference_moment_Nmm']/info['section_for_PM']['first_yield_moment_Nmm']
                self.assertAlmostEqual(util, 1.0, places=9)

    def test_collect_combines_gmnia_and_elastic_points(self):
        template, segments = fake_template()
        with tempfile.TemporaryDirectory() as root:
            ref = pm.section_reference(segments, 2., [0.])
            pm.write_section_reference(root, ref)
            with open(os.path.join(root, 'STEP5_PM_points.csv'), 'w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['model', 'load_case', 'bending_axis_deg', 'pm_angle_deg',
                                                            'P_kN', 'M_kNm', 'P_over_Py', 'M_over_My', 'outcome'])
                writer.writeheader()
                writer.writerow(dict(model='PM_P00_PERFECT', load_case='combined', bending_axis_deg='',
                                     pm_angle_deg=0, P_kN=100, M_kNm=0, P_over_Py=.6, M_over_My=0,
                                     outcome='CRITERION_REACHED'))
                writer.writerow(dict(model='PM_A000_P45_PERFECT', load_case='combined', bending_axis_deg=0,
                                     pm_angle_deg=45, P_kN=50, M_kNm=5, P_over_Py=.3, M_over_My=.3,
                                     outcome='CRITERION_REACHED'))
            folder = os.path.join(root, 'PMB_A000_P45')
            os.makedirs(folder)
            with open(os.path.join(folder, 'PMB_A000_P45_build.json'), 'w') as stream:
                json.dump(dict(load_case=dict(neutral_axis_angle_deg=0., pm_angle_deg=45.,
                                              reference_axial_force_mesh_N_per_MPa=400.,
                                              reference_moment_mesh_Nmm_per_MPa=1e4)), stream)
            with open(os.path.join(folder, 'PMB_A000_P45.dat'), 'w') as stream:
                stream.write(' MODE NO      EIGENVALUE\n\n\n       1       387.73    \n       2       389.13\n\n'
                             '  OTHER TEXT\n')
            out = os.path.join(root, 'diagram')
            result = pm.collect(pm.parse_arguments(['--mode', 'collect', '--roots', root, '--output-dir', out,
                                                    '--fy', '240']))
            self.assertEqual(result['points'], 3)
            with open(result['csv'], encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            elastic = [r for r in rows if r['source'] == 'ELASTIC_BUCKLING'][0]
            self.assertAlmostEqual(float(elastic['P_kN']), 387.73*400./1e3)
            self.assertAlmostEqual(float(elastic['M_kNm']), 387.73*1e4/1e6)
            self.assertTrue(os.path.isfile(os.path.join(out, 'PM_interaction_A000.png')))

    def test_collect_keeps_skip_datasets_and_section_normalizations_separate(self):
        from pathlib import Path
        template,segments=fake_template()
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as d:
            root=Path(d)
            for count,thickness in [(0,2.),(1,4.)]:
                dataset=root/('skip_%d'%count);dataset.mkdir()
                pm.write_section_reference(str(dataset),pm.section_reference(segments,thickness,[0.]))
                job='PMB_A000_P45';folder=dataset/job;folder.mkdir()
                (folder/(job+'_build.json')).write_text(json.dumps(dict(load_case=dict(
                    neutral_axis_angle_deg=0.,pm_angle_deg=45.,reference_axial_force_mesh_N_per_MPa=400.,
                    reference_moment_mesh_Nmm_per_MPa=1e4))))
                (folder/(job+'.dat')).write_text('MODE NO      EIGENVALUE\n\n       1       100.0\n')
            args=pm.parse_arguments(['--mode','collect','--roots',str(root),str(root/'skip_0'),
                                     '--output-dir',str(root/'diagram'),'--fy','240'])
            with mock.patch('abaqus_pm_demands.write_readable_plots',return_value=[]) as plot:
                result=pm.collect(args)
            self.assertEqual(result['points'],2)
            with open(result['csv'],encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(len({row['dataset'] for row in rows}),2)
            ordered=sorted(rows,key=lambda r:r['dataset'])
            self.assertAlmostEqual(float(ordered[1]['Py_kN']),2*float(ordered[0]['Py_kN']))
            self.assertAlmostEqual(float(ordered[1]['P_over_Py']),.5*float(ordered[0]['P_over_Py']))
            self.assertEqual(plot.call_args.args[2],{},'No shared plastic reference for differing sections')

    def test_dataset_identity_is_not_a_job_or_shared_leaf_folder_name(self):
        self.assertNotEqual(pm.interaction_dataset(dict(source='GMNIA',folder='skip_0/Step5_PM')),
                            pm.interaction_dataset(dict(source='GMNIA',folder='skip_1/Step5_PM')))
        self.assertEqual(pm.interaction_dataset(dict(source='ELASTIC_BUCKLING',folder='skip_0/PMB_A000_P45',
                                                     model='PMB_A000_P45')),os.path.abspath('skip_0'))

    def test_plots_do_not_connect_different_skip_configurations(self):
        import matplotlib.pyplot as plt
        from pathlib import Path
        axes=[];original=plt.subplots
        def capture(*args,**kwargs):
            fig,ax=original(*args,**kwargs);axes.append(ax);return fig,ax
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as d:
            points=[]
            for count,offset in [(0,0.),(1,10.)]:
                for i,psi in enumerate((30.,45.,60.)):
                    points.append(dict(source='GMNIA',source_case='LG_mp',load_case='combined',
                        folder=str(Path(d)/('skip_%d'%count)/'Step5_PM'),bending_axis_deg=0.,
                        pm_angle_deg=psi,M_kNm=offset+i+1.,P_kN=3.-i,
                        selection_kind='VERIFIED_PEAK',capacity_verified=True))
            with mock.patch.object(plt,'subplots',side_effect=capture):
                pm.plot_interaction(d,points,{})
            lines=[line for line in axes[0].get_lines() if line.get_label().startswith('GMNIA')]
            self.assertEqual(len(lines),2)
            self.assertTrue(all(len(line.get_xdata())==3 for line in lines))
            self.assertTrue(any(max(line.get_xdata())<10. for line in lines))
            self.assertTrue(any(min(line.get_xdata())>10. for line in lines))

    def test_first_eigenvalue_reads_abaqus_dat_table(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'x.dat')
            with open(path, 'w') as stream:
                stream.write('   E I G E N V A L U E    O U T P U T\n\n MODE NO      EIGENVALUE\n\n\n'
                             '       1      -12.5    \n       2       387.73    \n       3       392.17\n\n'
                             '                       P A R T I C I P A T I O N\n')
            self.assertEqual(pm.first_eigenvalue(path), 387.73)

    def test_buckling_orchestration_writes_one_folder_per_ray(self):
        import sys
        import types
        template, segments = fake_template()
        instances, unused_segments, unused = _fake_bending_mesh()

        class Repo(dict):
            pass

        class Model(object):
            def __init__(self):
                self.loads = Repo(COMP=1)
                self.steps = Repo(Initial=1, Buckle=1)
                self.fieldOutputRequests = Repo(old=1)
                self.historyOutputRequests = Repo(old=1)
                self.boundaryConditions = Repo(('BC%d' % i, 1) for i in range(9))
                self.rootAssembly = NS(instances=instances, sets={},
                                       SetFromNodeLabels=lambda name, nodeLabels: self.rootAssembly.sets.__setitem__(
                                           name, NS(name=name)))
                self.created = []

            def BuckleStep(self, **kw):
                self.steps['Buckle'] = kw

            def FieldOutputRequest(self, **kw):
                self.fieldOutputRequests[kw['name']] = kw

            def ConcentratedForce(self, name, **kw):
                self.loads[name] = kw

        written = {}

        class Database(object):
            def __init__(self):
                self.models = {'JOB': Model()}
                self.jobs = {}

            def Model(self, name, objectToCopy):
                self.models[name] = Model()
                return self.models[name]

            def Job(self, name, **kw):
                model = self.models[kw['model']]
                def write(consistencyChecking=None):
                    written['model'] = model
                    lines = ['*Heading', '*Step, name=Buckle', '*Buckle, eigensolver=LANCZOS', '20, 0., , , ']
                    for node in template['nodes']:
                        lines += ['** Name: %s   Type: Concentrated force' % node['load'], '*Cload',
                                  '%s, 3, 1.' % node['set']]
                    lines += ['*End Step', '']
                    with open(name+'.inp', 'w') as stream:
                        stream.write('\n'.join(lines))
                return NS(writeInput=write)

            def close(self):
                pass

        constants = types.ModuleType('abaqusConstants')
        for key in ('ON', 'LANCZOS', 'PERCENTAGE', 'FULL', 'SINGLE', 'UNIFORM', 'OFF'):
            setattr(constants, key, key)
        modules = {'caeModules': types.ModuleType('caeModules'), 'abaqusConstants': constants,
                   'abaqus': NS(openMdb=lambda pathName: Database())}
        build = dict(job_name='JOB', boundary_conditions=9, loads=8, source_inputs=dict(thickness_mm=2.))
        state = dict(settings=dict(nodal_precision='full'))
        open_working_mdb=pm.contract.open_working_mdb
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, 'JOB.cae'), 'w').close()
            out = os.path.join(root, 'buckling')
            with mock.patch.dict(sys.modules, modules), \
                    mock.patch.object(pm.contract,'open_working_mdb',
                                      side_effect=lambda source:open_working_mdb(source,scratch_dir=root)), \
                    mock.patch.object(pm, 'reference_section',
                                      return_value=(state, dict(build=build, job_name='JOB'), segments, 2.)):
                args = pm.parse_arguments(['--mode', 'buckling', '--run-dir', root, '--output-dir', out,
                                           '--pm-angles', '0,60', '--n-modes', '30'])
                pm.run_buckling(args)
            model = written['model']
            self.assertEqual(model.steps['Buckle']['minEigen'], 0.)
            self.assertEqual(model.steps['Buckle']['numEigen'], 30)
            self.assertEqual(sorted(model.fieldOutputRequests), ['ModeShapes'])
            self.assertNotIn('COMP', model.loads)
            folders = sorted(f for f in os.listdir(out) if os.path.isdir(os.path.join(out, f)))
            self.assertEqual(folders, ['PMB_A000_P60', 'PMB_A045_P60', 'PMB_P00'])
            for folder in folders:
                with open(os.path.join(out, folder, folder+'_build.json')) as stream:
                    build_json = json.load(stream)
                with open(os.path.join(out, folder, folder+'.inp')) as stream:
                    values = cload_values(stream.read())
                lc = build_json['load_case']
                bottom = [n for n in template['nodes'] if n['side'] == 0]
                self.assertAlmostEqual(sum(values[n['load']] for n in bottom),
                                       lc['reference_axial_force_mesh_N_per_MPa'], places=6)
                self.assertEqual(build_json['job_name'], folder)
                self.assertTrue(os.path.isfile(os.path.join(out, folder, 'abaqus_v6.env')))
            with open(os.path.join(out, 'run_pm_buckling.bat')) as stream:
                self.assertIn('job=PMB_A045_P60 input=PMB_A045_P60.inp', stream.read())



class SofteningResponseSelectionTests(unittest.TestCase):
    def response(self, x, loads):
        return [dict(frame=i, lpf=float(load), shortening_mm=float(displacement))
                for i, (displacement, load) in enumerate(zip(x, loads))]

    def test_persistent_transition_into_softening_is_a_provisional_stiffness_change(self):
        import numpy as np
        x = np.linspace(0., 1.25, 201)
        loads = np.minimum(x, 1.)-.025*np.maximum(x-1., 0.)
        point = pm.select_response_point(self.response(x, loads), 'shortening_mm', False)
        self.assertIsNotNone(point)
        self.assertEqual(point['selection_kind'], 'SIGNIFICANT_STIFFNESS_CHANGE')
        self.assertEqual(point['row']['frame'], 160)
        self.assertLess(point['selection_diagnostics']['tangent_ratio'], 0.)
        self.assertTrue(point['provisional'])
        self.assertFalse(point['capacity_verified'])
        self.assertLess(point['row']['frame'], len(loads)-1)

    def test_one_increment_negative_tangent_is_not_a_stiffness_change(self):
        import numpy as np
        x = np.linspace(0., 1., 201)
        loads = x.copy()
        loads[175] -= .02
        point = pm.select_response_point(self.response(x, loads), 'shortening_mm', False)
        self.assertEqual(point['selection_kind'], 'INCOMPLETE_ASCENDING_ENDPOINT')
        self.assertEqual(point['row']['frame'], 200)

    def test_smooth_curvature_into_a_shallow_descending_branch_is_not_a_knee(self):
        import numpy as np
        x = np.linspace(0., 1.2, 201)
        loads = 2.*x-x*x
        point=pm.select_response_point(self.response(x, loads), 'shortening_mm', False)
        self.assertEqual(point['selection_kind'],'UNVERIFIED_OBSERVED_MAXIMUM')
        self.assertEqual(point['row']['frame'],int(np.argmax(loads)))
        self.assertFalse(point['capacity_verified'])
        self.assertIsNone(pm.select_response_point(self.response(x, loads), 'shortening_mm', False, False))

class DemandOverlayTests(unittest.TestCase):
    def test_failed_descending_ray_uses_shared_provisional_selection_and_connection(self):
        from matplotlib.figure import Figure
        from pathlib import Path
        root = self.fixture(); source = root/'hss'; source.mkdir()
        name = 'PM_A000_P30_PERFECT'
        info = dict(stage=5, load_case=dict(type='combined'), source_case='PERFECT',
                    bending_axis_deg=0., pm_angle_deg=30., reference_axial_force_N=100000.,
                    reference_moment_Nmm=10000000., settings=dict(fy=240.))
        (source/(name+'_step5.json')).write_text(json.dumps(info))
        (source/(name+'.odb')).write_bytes(b'fixture: native ODB boundary, not opened')
        (source/(name+'.msg')).write_text('***ERROR: TIME INCREMENT REQUIRED IS LESS THAN THE MINIMUM SPECIFIED')
        fields = ['frame','lpf','extreme_fibre_shortening_mm','force_kN','moment_kNm','utilization',
                  'shortening_mm','rotation_mrad']
        rows = []
        for i in range(201):
            x = i*1.2/200.; load = 2.*x-x*x
            rows.append(dict(zip(fields,(i,load,x,load*100.,load*10.,load,x,x))))
        with (source/(name+'_pm_response.csv')).open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        (source/(name+'_pm_response.json')).write_text(json.dumps(dict(
            model=name,step='GMNIA',frames=len(rows),partial=False)))
        record=dict(model=name,analysis_state='FAILED',outcome='SOLVER_ERROR',curve_available=True,
                    criterion_reached=False,termination_requested=False,partial=False)
        (source/'STEP5_queue_status.json').write_text(json.dumps([record]))
        before={p:p.read_bytes() for p in source.iterdir()}
        self.assertEqual(pm.select_response_point(rows,'extreme_fibre_shortening_mm',False)['selection_kind'],
                         'UNVERIFIED_OBSERVED_MAXIMUM')
        captured=[]
        with mock.patch.object(Figure,'savefig',autospec=True,side_effect=lambda fig,*a,**k:captured.append(fig)):
            result=pm.collect(pm.parse_arguments(['--mode','collect','--roots',str(source),
                '--dataset-labels','HSS','--output-dir',str(root/'diagram')]))
        self.assertEqual(result['points'],1)
        with open(result['csv'],encoding='utf-8-sig') as stream:
            selected=list(csv.DictReader(stream))
        self.assertEqual(len(selected),1)
        with (root/'diagram'/'PM_observed_points.csv').open(encoding='utf-8-sig') as stream:
            observations=list(csv.DictReader(stream))
        self.assertEqual(observations,[])
        peak=max(rows,key=lambda row:row['lpf']); observed=selected[0]
        self.assertAlmostEqual(float(observed['P_kN']),peak['force_kN'])
        self.assertAlmostEqual(float(observed['M_kNm']),peak['moment_kNm'])
        self.assertEqual(int(observed['frame']),peak['frame'])
        self.assertEqual(observed['selection_kind'],'UNVERIFIED_OBSERVED_MAXIMUM')
        self.assertEqual(observed['capacity_verified'],'False')
        figure=captured[0]
        markers=[line for line in figure.axes[0].lines if line.get_gid()=='readable-observed-point']
        self.assertEqual(len(markers),1); self.assertEqual(markers[0].get_marker(),'x')
        self.assertIn(' O',figure.axes[1].tables[0].get_celld()[1,1].get_text().get_text())
        self.assertFalse(any(len(line.get_xdata())>1 for line in figure.axes[0].lines))
        audit=json.loads((root/'diagram'/'PM_analysis_audit.json').read_text())
        self.assertEqual(audit['summary']['observed_unselected_points'],0)
        self.assertEqual(audit['summary']['omitted_points'],0)
        response_manifest=root/'diagram'/'PM_model_response_plots.json'
        paired=json.loads(response_manifest.read_text())['models']
        self.assertEqual(len(paired),1)
        self.assertEqual(paired[0]['model'],name)
        self.assertEqual(paired[0]['frames'],len(rows))
        self.assertEqual(paired[0]['source_csv'],str(source/(name+'_pm_response.csv')))
        self.assertEqual(paired[0]['outcome'],'SOLVER_ERROR')
        self.assertTrue(Path(paired[0]['workbook']).is_file())
        self.assertTrue(all('_axial_bending_response.' in p for p in paired[0]['files']))
        # Cache repair/reuse changes plot artifacts only, never the original CSV/ODB.
        import abaqus_step5_force_displacement_core as fd
        from pathlib import Path
        for path in paired[0]['files']:Path(path).write_bytes(b'test image boundary')
        row=dict(selected[0],source='GMNIA',folder=str(source),dataset_label='HSS')
        with mock.patch.object(fd,'write_response_pair',side_effect=AssertionError('unnecessary replot')):
            self.assertEqual(fd.collect_response_pairs(str(root/'diagram'),[row]),paired[0]['files']+[paired[0]['workbook']])
            Path(paired[0]['workbook']).unlink()
            fd.collect_response_pairs(str(root/'diagram'),[row])
            self.assertTrue(Path(paired[0]['workbook']).is_file())
            Path(paired[0]['workbook']).write_bytes(b'interrupted workbook')
            fd.collect_response_pairs(str(root/'diagram'),[row])
            self.assertTrue(fd.response_workbook_valid(paired[0]['workbook']))
        # A partially written plot cache is disposable, not scientific source data.
        response_manifest.write_text('{interrupted cache')
        with mock.patch.object(fd,'write_response_pair',wraps=fd.write_response_pair) as render, \
                mock.patch.object(Figure,'savefig',autospec=True):
            fd.collect_response_pairs(str(root/'diagram'),[row])
            self.assertEqual(render.call_count,1)
        self.assertEqual(before,{p:p.read_bytes() for p in source.iterdir()})
        (source/(name+'.lck')).write_text('running')
        with self.assertRaisesRegex(ValueError,'locked/running'):
            pm.refresh_saved_pm_points(str(source),[],[])

    def test_observed_maxima_enter_connections_and_remain_unverified(self):
        from matplotlib.figure import Figure
        import abaqus_pm_demands as demand
        points=[dict(source='GMNIA',dataset='hss',source_case='PERFECT',bending_axis_deg=0.,
                     pm_angle_deg=angle,M_kNm=m,P_kN=p,selection_kind=kind,capacity_verified=False)
                for angle,m,p,kind in ((0.,0.,100.,'INCOMPLETE_ASCENDING_ENDPOINT'),
                                       (30.,4.,70.,'UNSELECTED_OBSERVED_MAXIMUM'),
                                       (90.,10.,0.,'INCOMPLETE_ASCENDING_ENDPOINT'))]
        groups=demand._readable_groups(points,0.);groups[0]['expected_angles']=[0.,30.,90.]
        fig=Figure();ax=fig.subplots();demand._readable_curves(ax,groups)
        connections=[line for line in ax.lines if len(line.get_xdata())>1]
        self.assertEqual(len(connections),2)
        self.assertEqual([list(line.get_xdata()) for line in connections],[[0.,4.],[4.,10.]])
        self.assertEqual([list(line.get_ydata()) for line in connections],[[100.,70.],[70.,0.]])
        self.assertEqual(sum(line.get_gid()=='readable-observed-point' for line in ax.lines),1)

    def test_unprocessed_planned_rays_are_reported_and_connections_mark_the_gap(self):
        from matplotlib.figure import Figure
        import abaqus_pm_demands as demand
        root = self.fixture()
        for manifest_name in ('pm_manifest.json', 'hss_manifest.json'):
            with self.subTest(manifest=manifest_name):
                source = root/manifest_name.replace('.json', '')
                source.mkdir()
                (source/'STEP5_PM_points.csv').write_text(
                    'model,source_case,load_case,bending_axis_deg,pm_angle_deg,P_kN,M_kNm\n'
                    'PM_P00_PERFECT,PERFECT,combined,0,0,100,0\n'
                    'PM_A000_P90_PERFECT,PERFECT,combined,0,90,0,10\n')
                jobs = [dict(job=name, case='PERFECT', bending_axis_deg=0., pm_angle_deg=angle)
                        for name,angle in (('PM_P00_PERFECT',0.), ('PM_A000_P45_PERFECT',45.),
                                           ('PM_A000_P90_PERFECT',90.))]
                manifest = dict(mode='gmnia', jobs=jobs)
                if manifest_name == 'hss_manifest.json':
                    for job in jobs:
                        (source/(job['job']+'_step5.json')).write_text(json.dumps(dict(
                            stage=5, source_case=job['case'], bending_axis_deg=0., pm_angle_deg=job['pm_angle_deg'])))
                    manifest['jobs'] = [job['job'] for job in jobs]
                (source/manifest_name).write_text(json.dumps(manifest))
                captured = []
                with mock.patch.object(Figure, 'savefig', autospec=True,
                                       side_effect=lambda fig,*a,**kw: captured.append(fig)):
                    result = pm.collect(pm.parse_arguments(['--mode','collect','--roots',str(source),
                        '--dataset-labels','No plates','--output-dir',str(source/'diagram')]))
                with open(result['audit'], encoding='utf-8') as stream:
                    audit = json.load(stream)
                self.assertEqual(audit['summary']['analyses'], 3)
                self.assertEqual(audit['summary']['omitted_points'], 1)
                missing = next(row for row in audit['analyses'] if not row['selected'])
                self.assertEqual((missing['model'], missing['pm_angle_deg']), ('PM_A000_P45_PERFECT',45.))
                model = captured[0]
                self.assertTrue(any(line.is_dashed() for line in model.axes[0].lines))
                self.assertEqual(model.axes[1].tables[0].get_celld()[2,1].get_text().get_text(), '--')

    def test_approved_layout_separates_demand_groups_and_publishes_primary_aliases(self):
        import copy
        from pathlib import Path
        from matplotlib.figure import Figure
        import matplotlib.pyplot as plt
        import abaqus_pm_demands as demand
        root = self.fixture()
        points = [dict(source='GMNIA', source_case='PERFECT', dataset='one',
                       dataset_label='HSS', bending_axis_deg=0., pm_angle_deg=a,
                       P_kN=p, M_kNm=m, selection_kind='VERIFIED_PEAK', capacity_verified=True)
                  for a,p,m in ((0., 100., 0.), (45., 60., 5.), (90., 0., 7.))]
        demands = demand.read_demands(Path(__file__).resolve().parent/'building_8story_demands.csv')
        before = copy.deepcopy((points, demands)); captured = {}
        def capture(fig, path, **kwargs):
            if str(path).endswith('.png'): captured[Path(path).name] = fig
        with mock.patch.object(Figure, 'savefig', autospec=True, side_effect=capture):
            paths = demand.write_readable_plots(root, points, {}, demands=demands, publish_primary=True)
        self.assertIn(str(root/'PM_interaction.png'), paths)
        self.assertIn('PM_interaction_A000.png', captured)
        self.assertTrue(captured['01_PM_models_readable.png'].axes[1].tables)
        panels = captured['02_PM_demands_readable.png'].axes
        self.assertEqual(len(panels), 8)
        for panel in panels:
            coordinates = [tuple(x) for collection in panel.collections for x in collection.get_offsets()]
            self.assertEqual(len(coordinates), 8)
            self.assertEqual({t.get_text() for t in panel.texts}, {str(i) for i in range(1,9)})
        self.assertEqual((points, demands), before)
        self.assertFalse(plt.get_fignums())

    def fixture(self):
        from pathlib import Path
        import shutil, uuid
        root=Path(__file__).resolve().parent/('demand_test_'+uuid.uuid4().hex)
        root.mkdir();self.addCleanup(shutil.rmtree,root)
        return root

    def test_demands_preserve_signed_axial_force_and_missing_paired_moment(self):
        import abaqus_pm_demands as demand
        root=self.fixture();path=root/'demand.csv'
        path.write_text('id,floor,column,load_case,P_kN,M_major_kNm,M_minor_kNm,notes\n'
                        'G-I-1,1,interior,gravity,1804.4236,9.80665,,approximate\n'
                        'E-C-T1,1,corner,seismic,-912.01845,,,no paired moment\n')
        rows=demand.read_demands(path)
        self.assertEqual(rows[1]['P_kN'],-912.01845)
        self.assertIsNone(rows[1]['M_major_kNm'])
        path.write_text(path.read_text().replace('1804.4236','nan'))
        with self.assertRaisesRegex(ValueError,'finite'):
            demand.read_demands(path)

    def test_demand_projection_is_display_only_and_never_invents_a_zero_moment(self):
        import abaqus_pm_demands as demand
        import matplotlib.pyplot as plt
        rows=[dict(id='E-C-1',floor=1,column='corner',load_case='seismic',P_kN=1471.,
                   M_major_kNm=264.8,M_minor_kNm=93.2,notes='unpaired maxima'),
              dict(id='E-C-T1',floor=1,column='corner',load_case='seismic',P_kN=-912.,
                   M_major_kNm=None,M_minor_kNm=None,notes='no paired M')]
        fig,ax=plt.subplots()
        try:
            summary=demand.draw_overlay(ax,rows)
            self.assertEqual(summary,dict(plotted_components=2,axial_only_cases=1))
            self.assertTrue(any('orientation unassigned' in item.get_text() for item in ax.texts))
            coordinates=[tuple(point) for item in ax.collections for point in item.get_offsets()]
            self.assertIn((264.8,1471.),coordinates)
            self.assertIn((93.2,1471.),coordinates)
            self.assertFalse(any(point[1]<0. for point in coordinates))
        finally:plt.close(fig)

    def test_report_input_and_collector_keep_demands_separate_from_capacities(self):
        from pathlib import Path
        import abaqus_pm_demands as demand
        report=Path(__file__).resolve().parent/'building_8story_demands.csv'
        demands=demand.read_demands(report)
        self.assertEqual(len(demands),56)
        self.assertEqual(sum(row['M_major_kNm'] is None for row in demands),8)
        first=next(row for row in demands if row['id']=='G-I-1')
        self.assertAlmostEqual(first['P_kN'],184.*9.80665)
        self.assertAlmostEqual(next(row for row in demands if row['id']=='E-P-1')['M_major_kNm'],51.*9.80665)
        root=self.fixture(); source=root/'source';source.mkdir()
        csv_path=source/'STEP5_PM_points.csv'
        csv_path.write_text('model,source_case,load_case,bending_axis_deg,pm_angle_deg,P_kN,M_kNm,outcome\n'
                            'PM_A000_P30_PERFECT,PERFECT,combined,0,30,12,2,FAILED\n')
        before=csv_path.read_bytes(); output=root/'diagram'
        args=pm.parse_arguments(['--mode','collect','--roots',str(source),
                                 '--output-dir',str(output),'--demand-csv',str(report)])
        supplements=[str(output/(stem+'.'+ext)) for stem in ('01_PM_models_readable','02_PM_demands_readable')
                     for ext in ('png','svg','pdf')]
        with mock.patch.object(pm,'plot_interaction',return_value=[]) as plot, \
                mock.patch.object(demand,'write_readable_plots',return_value=supplements) as readable:
            result=pm.collect(args)
        self.assertEqual(result['points'],1)
        plot.assert_not_called()  # dimensional primary figures use the approved layout
        self.assertEqual(result['plots'],supplements)
        self.assertEqual(len(readable.call_args.kwargs['demands']),56)
        self.assertTrue(readable.call_args.kwargs['publish_primary'])
        with (output/'PM_demand_points.csv').open(encoding='utf-8-sig',newline='') as stream:
            exported=list(csv.DictReader(stream))
        self.assertEqual(sum(row['display_status']=='axial_only_no_paired_M' for row in exported),8)
        self.assertEqual(csv_path.read_bytes(),before)

    def test_readable_supplement_uses_each_axis_and_preserves_physical_points_and_status(self):
        import copy
        from pathlib import Path
        import matplotlib.pyplot as plt
        from matplotlib.figure import Figure
        import abaqus_pm_demands as demand
        root=self.fixture();points=[]
        for axis in (0.,45.):
            for case,scale in (('PERFECT',1.),('LG_mp',.98)):
                for angle,p,m in ((0.,2400.,0.),(30.,1500.,180.),(90.,0.,500.)):
                    points.append(dict(source='GMNIA',source_case=case,dataset=str(root/case),
                        dataset_label=case,bending_axis_deg=axis,pm_angle_deg=angle,
                        P_kN=p*scale,M_kNm=m*scale,selection_kind='VERIFIED_PEAK' if angle==30. else
                        'INCOMPLETE_ASCENDING_ENDPOINT',capacity_verified=angle==30.))
        demands=[dict(id='gI1',floor=1,column='interior',load_case='gravity',P_kN=5000.,
                      M_major_kNm=800.,M_minor_kNm=None,notes='orientation unknown'),
                 dict(id='eC2',floor=2,column='corner',load_case='seismic',P_kN=-500.,
                      M_major_kNm=30.,M_minor_kNm=10.,notes='paired example'),
                 dict(id='eC3',floor=3,column='corner',load_case='seismic',P_kN=-900.,
                      M_major_kNm=None,M_minor_kNm=None,notes='unknown M')]
        before=copy.deepcopy((points,demands));figures=[]
        def capture(fig,path,**kwargs):
            if str(path).endswith('.png'):figures.append((Path(path).name,fig))
        reference=dict(AsFy_kN=2600.,axes={'x':dict(SFy_kNm=650.,ZFy_kNm=900.),
                                          'z':dict(SFy_kNm=600.,ZFy_kNm=850.)})
        with mock.patch.object(Figure,'savefig',autospec=True,side_effect=capture):
            paths=demand.write_readable_plots(root,points,{},demands=demands,report_reference=reference)
        plot_paths=[p for p in paths if Path(p).suffix!='.json']
        self.assertEqual(len(plot_paths),12)
        self.assertEqual({Path(p).suffix for p in plot_paths},{'.png','.svg','.pdf'})
        diagnostic=json.loads((root/'PM_convexity_diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual(len(diagnostic['groups']),4)
        # The middle sample is below the straight line between the intercepts.
        self.assertEqual({row['status'] for row in diagnostic['groups']},{'NONCONVEX_SAMPLES'})
        self.assertTrue(any('_A045' in p for p in paths))
        for name,fig in figures:
            main=fig.axes[0]
            self.assertGreater(main.get_xlim()[1],500.)
            self.assertGreater(main.get_ylim()[1],2400.)
            if name.startswith('02_'):
                self.assertGreater(main.get_xlim()[1],800.)
                self.assertGreater(main.get_ylim()[1],5000.)
                self.assertTrue(any(ax.get_ylim()[0]<-500. for ax in fig.axes))
                positions=[tuple(v) for ax in fig.axes for collection in ax.collections for v in collection.get_offsets()]
                self.assertNotIn((0.,-900.),positions)
            else:
                native=[line for line in main.lines if line.get_gid()=='readable-fe-point']
                self.assertEqual(len(native),6)
                self.assertEqual(sum(line.get_markerfacecolor()!='white' for line in native),2)
        self.assertEqual((points,demands),before)
        self.assertFalse(plt.get_fignums())

    def test_readable_groups_do_not_share_axis_owned_pure_compression_with_other_specimens(self):
        import abaqus_pm_demands as demand
        points=[dict(source='GMNIA',source_case='PERFECT',dataset='axis_0',dataset_label='HSS',
                     bending_axis_deg='',P_kN=100.,M_kNm=0.,selection_kind='VERIFIED_PEAK',capacity_verified=False),
                dict(source='GMNIA',source_case='PERFECT',dataset='axis_0',dataset_label='HSS',
                     bending_axis_deg=0.,P_kN=0.,M_kNm=10.,selection_kind='INCOMPLETE_ASCENDING_ENDPOINT',capacity_verified=False),
                dict(source='GMNIA',source_case='PERFECT',dataset='axis_45',dataset_label='HSS',
                     bending_axis_deg=45.,P_kN=20.,M_kNm=6.,selection_kind='SIGNIFICANT_LOAD_DROP',capacity_verified=False)]
        zero=demand._readable_groups(points,0.)
        diagonal=demand._readable_groups(points,45.)
        self.assertEqual(len(zero),1)
        self.assertEqual(len(zero[0]['values']),2)
        self.assertEqual(len(diagonal),1)
        self.assertEqual(len(diagonal[0]['values']),1)
        self.assertFalse(any(v['verified'] for group in zero+diagonal for v in group['values']))



class IndependentResponseSelectionTests(unittest.TestCase):
    def response(self, axial_knee=.72, bending_knee=.5, ratio=.5):
        import numpy as np
        loads=np.linspace(0.,1.,201)
        def displacement(p,knee):
            return min(p,knee)+max(0.,p-knee)/ratio
        return [dict(frame=i,lpf=float(p),force_kN=100.*p,moment_kNm=10.*p,
                     shortening_mm=displacement(p,axial_knee),
                     rotation_mrad=displacement(p,bending_knee)) for i,p in enumerate(loads)]

    def test_earliest_bending_knee_selects_one_synchronized_frame_before_peak(self):
        rows=self.response()
        selected=pm.select_pm_response_point(rows,True)
        self.assertEqual(selected['selection_kind'],'SIGNIFICANT_STIFFNESS_CHANGE')
        self.assertEqual(selected['selection_response'],'rotation_mrad')
        self.assertLessEqual(abs(selected['row']['frame']-100),2)
        self.assertEqual(selected['row'],rows[selected['row']['frame']])
        self.assertFalse(selected['capacity_verified'])
        self.assertTrue(selected['provisional'])

    def test_axial_knee_can_precede_bending_and_is_scale_invariant(self):
        rows=self.response(.5,.72)
        expected=pm.select_pm_response_point(rows,True)
        for r in rows:
            r['shortening_mm']*=1e-4;r['rotation_mrad']*=1e4
        selected=pm.select_pm_response_point(rows,True)
        self.assertEqual(selected['selection_response'],'shortening_mm')
        self.assertEqual(selected['row']['frame'],expected['row']['frame'])
        self.assertLessEqual(abs(selected['row']['frame']-100),2)

    def test_unloaded_channel_is_ignored_in_both_pure_endpoints(self):
        for force,displacement,loaded in [('moment_kNm','rotation_mrad','shortening_mm'),
                                          ('force_kN','shortening_mm','rotation_mrad')]:
            rows=self.response(.55,.55)
            for r in rows:
                r[force]=0.;r[displacement]=1e-14*(r['frame']%7)
            selected=pm.select_pm_response_point(rows,True)
            self.assertEqual(selected['selection_response'],loaded)
            self.assertIn(displacement,selected['selection_diagnostics']['inactive_channels'])
            self.assertLess(selected['row']['frame'],150)

    def test_small_change_and_smooth_curvature_do_not_replace_peak(self):
        import numpy as np
        for variant in ('small','quadratic','single_dip'):
            rows=self.response(.5,.5,.85)
            if variant!='small':
                for i,r in enumerate(rows):
                    x=i/200.;p=2*x-x*x if variant=='quadratic' else x-(.02 if i==170 else 0.)
                    r.update(lpf=p,force_kN=100*p,moment_kNm=10*p,
                             shortening_mm=x,rotation_mrad=10*x)
            selected=pm.select_pm_response_point(rows,True)
            self.assertEqual(selected['selection_kind'],'VERIFIED_PEAK',variant)
            self.assertEqual(selected['row']['frame'],200)

    def test_queue_uses_the_same_frame_for_all_geometries_cases_and_terminal_states(self):
        import abaqus_step5_queue as queue
        rows=self.response()
        for variant in ('HSS','No plates','With plates'):
            for case in ('PERFECT','LG_mp'):
                failed=case=='LG_mp'
                result=dict(model='PM_A000_P30_'+case,
                    capacity_verified=not failed,analysis_state='FAILED' if failed else 'TERMINATED',
                    outcome='SOLVER_ERROR' if failed else 'CONTROLLED_TERMINATION',
                    path_review_required=True,postpeak_path_check=dict(status='RETRACE_SUSPECTED'))
                info=dict(load_case=dict(type='combined'),source_case=case,variant=variant,
                          reference_axial_force_N=100000.,reference_moment_Nmm=10000000.,
                          settings=dict(fy=240.))
                queue.select_pm_result(result,info,rows)
                point=result['pm_point'];frame=point['frame']
                self.assertLessEqual(abs(frame-100),2)
                self.assertEqual(point['P_kN'],rows[frame]['force_kN'])
                self.assertEqual(point['M_kNm'],rows[frame]['moment_kNm'])
                self.assertEqual(point['selection_response'],'rotation_mrad')
                self.assertEqual(point['selection_policy'],'response-v4-dual')
                self.assertFalse(point['capacity_verified'])
                self.assertTrue(result['path_review_required'])
                self.assertEqual(result['capacity_verified'],not failed)
                self.assertIn('postpeak_path_check',json.loads(point['selection_diagnostics']))

    def test_small_startup_reversal_does_not_hide_a_later_bending_knee(self):
        rows=self.response(.85,.5)
        rows[1]['rotation_mrad']=-1e-7
        point=pm.select_pm_response_point(rows,True)
        self.assertEqual(point['selection_response'],'rotation_mrad')
        self.assertLessEqual(abs(point['row']['frame']-100),2)

    def test_later_sign_crossing_does_not_reverse_the_initial_branch_direction(self):
        rows=self.response()
        for i,r in enumerate(rows):
            r['force_kN']=0.
            if i>160:r['rotation_mrad']=rows[160]['rotation_mrad']-.08*(i-160)
        point=pm.select_pm_response_point(rows,True)
        self.assertEqual(point['selection_kind'],'SIGNIFICANT_STIFFNESS_CHANGE')
        self.assertEqual(point['selection_response'],'rotation_mrad')
        self.assertLessEqual(abs(point['row']['frame']-100),2)

    def test_simultaneous_events_retain_a_real_csv_key_and_both_triggers(self):
        rows=self.response(.5,.5)
        point=pm.select_pm_response_point(rows,True)
        self.assertIn(point['selection_response'],rows[0])
        self.assertEqual(point['selection_diagnostics']['trigger_channels'],
                         ['shortening_mm','rotation_mrad'])

    def test_recovered_drop_is_not_lost_when_rotation_reverses(self):
        loads=[i*.025 for i in range(37)]+[.89,.87,.85,.83,.81,.82,.84,.86,.89,.92,.96,1.]
        rows=[dict(frame=i,lpf=p,force_kN=100*p,moment_kNm=10*p,
                   shortening_mm=i*.03,rotation_mrad=(p if i<42 else .81+(i-41)*.02))
              for i,p in enumerate(loads)]
        point=pm.select_pm_response_point(rows,False)
        self.assertEqual(point['selection_kind'],'SIGNIFICANT_LOAD_DROP')
        self.assertEqual(point['row']['frame'],36)
        self.assertFalse(point['capacity_verified'])

    def test_live_response_is_not_selected_and_retracing_tail_does_not_move_onset(self):
        rows=self.response()
        self.assertIsNone(pm.select_pm_response_point(rows,False,False))
        first=pm.select_pm_response_point(rows,True)
        tail=[dict(r,frame=201+i) for i,r in enumerate(reversed(rows[130:190]))]
        selected=pm.select_pm_response_point(rows+tail,True)
        self.assertEqual(selected['row']['frame'],first['row']['frame'])

if __name__ == '__main__':
    unittest.main()
