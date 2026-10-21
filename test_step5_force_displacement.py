import math
import unittest
import abaqus_step5_force_displacement_core as fd


class ResponsePairPlotTests(unittest.TestCase):
    def test_live_reports_publish_beside_each_job_and_in_combined_diagram(self):
        import csv, json, os, tempfile
        from pathlib import Path
        from unittest import mock
        from matplotlib.figure import Figure
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root/'hss'; source.mkdir()
            name = 'PM_A000_P45_PERFECT'
            info = dict(stage=5,load_case=dict(type='combined'),source_case='PERFECT',pm_angle_deg=45.,
                        bending_axis_deg=0.,reference_axial_force_N=100000.,reference_moment_Nmm=10000000.,
                        settings=dict(postpeak_stop_ratio=.8,fy=240.))
            (source/(name+'_step5.json')).write_text(json.dumps(info))
            (source/(name+'.odb')).write_bytes(b'opaque ODB boundary; report must not open it')
            (source/(name+'.msg')).write_text('***ERROR: Process terminated by external request (SIGTERM or SIGINT received).')
            rows = [dict(frame=i,lpf=p,force_kN=100*p,moment_kNm=10*p,shortening_mm=i,
                         rotation_mrad=i,extreme_fibre_shortening_mm=i,utilization=p)
                    for i,p in enumerate((0.,.5,1.,.75))]
            with (source/(name+'_pm_response.csv')).open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            (source/(name+'_pm_response.json')).write_text(json.dumps(dict(model=name,step='GMNIA',frames=4,partial=False)))
            record=dict(model=name,outcome='CONTROLLED_TERMINATION',criterion_reached=True,
                        termination_requested=True,analysis_state='TERMINATED',postpeak_stop_ratio=.8,
                        partial=False,curve_available=True)
            (source/'STEP5_queue_status.json').write_text(json.dumps([record]))
            context=json.dumps(dict(output_dir=str(root/'diagram'),datasets=[dict(root=str(source),label='HSS')]))
            def save(figure,path,**unused): Path(path).write_bytes(b'test figure')
            with mock.patch.dict(os.environ,{'STEP5_LIVE_REPORT_CONTEXT':context}), \
                    mock.patch.object(Figure,'savefig',autospec=True,side_effect=save):
                fd.publish_queue_responses(source)
                for extension in ('png','svg','xlsx'):
                    self.assertTrue((source/(name+'_axial_bending_response.'+extension)).is_file())
                    self.assertEqual(len(list((root/'diagram'/'response_curves').rglob('*.'+extension))),1)
                self.assertTrue(fd.response_workbook_valid(str(source/(name+'_axial_bending_response.xlsx'))))
                self.assertTrue((root/'diagram'/'04_PM_response_paths.svg').is_file())
                self.assertTrue((source/'STEP5_response_paths.svg').is_file())
                saved = source/(name+'_axial_bending_response.xlsx')
                stamp = saved.stat().st_mtime_ns
                fd.publish_queue_responses(source)
                self.assertEqual(saved.stat().st_mtime_ns,stamp)

    def test_live_overview_retains_all_jobs_all_frames_and_solver_order(self):
        import csv, tempfile
        from pathlib import Path
        from unittest import mock
        from matplotlib.figure import Figure
        import matplotlib.pyplot as plt
        with tempfile.TemporaryDirectory(dir=__import__("pathlib").Path(__file__).parent) as directory:
            root = Path(directory)
            source = root/'response.csv'
            rows = self.rows()
            for i, row in enumerate(rows): row['lpf'] = (0., .5, 1., .8, .6)[i]
            with source.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
            entries = [dict(model='PM_%d'%i, dataset_label='HSS', source_csv=str(source),
                            outcome='SOLVER_ERROR', selected_frame=5, selection_kind='SIGNIFICANT_STIFFNESS_CHANGE',
                            selection_response='rotation_mrad') for i in range(7)]
            figures = []
            def save(figure, path, **unused):
                if str(path).endswith('.svg'): figures.append(figure)
            with mock.patch.object(Figure, 'savefig', autospec=True, side_effect=save):
                files = fd.write_response_path_overview(directory, entries)
            self.assertEqual(len(files), 4)
            self.assertEqual(sum(len(f.axes)//2 for f in figures), 7)
            curves = [line for f in figures for ax in f.axes for line in ax.lines
                      if line.get_gid() == 'response-path-curve']
            self.assertEqual(len(curves), 14)
            self.assertEqual(list(curves[0].get_xdata()), [r['rotation_mrad'] for r in rows])
            self.assertEqual(list(curves[1].get_xdata()), [r['frame'] for r in rows])
            self.assertEqual(list(curves[0].get_ydata()), [r['lpf'] for r in rows])
            self.assertFalse(plt.get_fignums())

    def test_excel_contains_all_frames_numeric_pairs_units_and_solver_status(self):
        from pathlib import Path
        import shutil,uuid,zipfile
        import xml.etree.ElementTree as ET
        from abaqus_report_reference import _Workbook
        root=Path(__file__).resolve().parent/('response_excel_test_'+uuid.uuid4().hex)
        root.mkdir();self.addCleanup(shutil.rmtree,root)
        path=root/'PM_A000_P75_PERFECT_axial_bending_response.xlsx'
        rows=self.rows()
        for i,row in enumerate(rows):row['lpf']=i/10.
        fd.write_response_workbook(str(path),rows,'PM_A000_P75_PERFECT',
            result=dict(outcome='SOLVER_ERROR',pm_point=dict(frame=5,capacity_verified=False,
                        selection_kind='UNVERIFIED_OBSERVED_MAXIMUM',selection_response='rotation_mrad',
                        selection_policy='response-v4-dual',selection_diagnostics='{"trigger_channels":["rotation_mrad"]}')),
                        source_csv='original.csv')
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            book=_Workbook(archive)
            self.assertEqual(set(book.sheets),{'Axial','Bending','Metadata'})
            for sheet,xkey,ykey in (('Axial','shortening_mm','force_kN'),('Bending','rotation_mrad','moment_kNm')):
                values=book.rows(sheet)
                tree=ET.fromstring(archive.read(book.sheets[sheet]))
                ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                data_rows=tree.findall('s:sheetData/s:row',ns)[1:]
                self.assertTrue(all(c.get('t') in (None,'n') for r in data_rows for c in r.findall('s:c',ns)))
                self.assertEqual(len(values),len(rows)+1)
                self.assertTrue('mm' in values[0]['B'] or 'mrad' in values[0]['B'])
                for exported,source in zip(values[1:],rows):
                    self.assertEqual(int(exported['A']),source['frame'])
                    self.assertAlmostEqual(float(exported['B']),source[xkey])
                    self.assertAlmostEqual(float(exported['C']),source[ykey])
                    self.assertAlmostEqual(float(exported['D']),source['lpf'])
            metadata={r['A']:r.get('B') for r in book.rows('Metadata')[1:]}
            self.assertEqual(metadata['Solver outcome'],'SOLVER_ERROR')
            self.assertEqual(metadata['Selected frame'],'5')
            self.assertEqual(metadata['Selected point verified'],'False')
            self.assertEqual(metadata['Selection response'],'rotation_mrad')
            self.assertEqual(metadata['Selection policy'],'response-v4-dual')
            self.assertIn('rotation_mrad',metadata['Selection diagnostics'])
            self.assertEqual(metadata['Source CSV'],'original.csv')

    def test_saved_nondefault_stop_ratio_is_preserved_during_pair_collection(self):
        from pathlib import Path
        import json,csv,shutil,uuid
        from matplotlib.figure import Figure
        from unittest import mock
        root=Path(__file__).resolve().parent/('response_pair_test_'+uuid.uuid4().hex)
        root.mkdir();self.addCleanup(shutil.rmtree,root)
        source=root/'source';source.mkdir();name='PM_A000_P45_PERFECT'
        info=dict(stage=5,load_case=dict(type='combined'),source_case='PERFECT',pm_angle_deg=45.,
                  bending_axis_deg=0.,reference_axial_force_N=100000.,reference_moment_Nmm=10000000.,
                  settings=dict(postpeak_stop_ratio=.8))
        (source/(name+'_step5.json')).write_text(json.dumps(info))
        (source/(name+'.odb')).write_bytes(b'opaque ODB boundary')
        (source/(name+'.msg')).write_text('***ERROR: Process terminated by external request (SIGTERM or SIGINT received).')
        rows=[dict(frame=i,lpf=p,force_kN=100*p,moment_kNm=10*p,shortening_mm=i,
                   rotation_mrad=i,extreme_fibre_shortening_mm=i,utilization=p)
              for i,p in enumerate((0.,.5,1.,.75))]
        with (source/(name+'_pm_response.csv')).open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        (source/(name+'_pm_response.json')).write_text(json.dumps(dict(model=name,step='GMNIA',frames=4,partial=False)))
        record=dict(model=name,outcome='CONTROLLED_TERMINATION',criterion_reached=True,
                    termination_requested=True,analysis_state='TERMINATED',postpeak_stop_ratio=.8,partial=False)
        (source/'STEP5_queue_status.json').write_text(json.dumps([record]))
        with mock.patch.object(Figure,'savefig',autospec=True):
            files=fd.collect_response_pairs(str(root/'diagram'),[dict(source='GMNIA',folder=str(source),model=name)])
        self.assertEqual(len(files),3)

    def test_frame_labels_cover_response_when_many_increments_cluster_near_the_peak(self):
        rows=[dict(frame=i,shortening_mm=i/5. if i<=5 else 1.+1e-6*(i-5),
                   force_kN=min(i/5.,1.),moment_kNm=0.) for i in range(101)]
        labels=fd.response_pair_samples(rows,'shortening_mm')
        self.assertGreaterEqual(len(labels),5)
        for group in labels:
            self.assertTrue(all(rows[i]['shortening_mm']==group['x'] for i in group['indices']))

    def rows(self):
        return [dict(frame=frame,shortening_mm=x,rotation_mrad=t,force_kN=p,moment_kNm=m)
                for frame,x,t,p,m in ((0,0.,0.,0.,0.),(2,.2,1.,50.,5.),(5,.6,3.,100.,10.),
                                       (8,.4,2.,80.,8.),(11,.9,0.,60.,6.))]

    def test_pair_preserves_solver_order_and_labels_actual_nonuniform_frames(self):
        from matplotlib.figure import Figure
        import matplotlib.pyplot as plt
        from unittest import mock
        rows=self.rows(); figures=[]
        with mock.patch.object(Figure,'savefig',autospec=True,
                               side_effect=lambda fig,*args,**kw:figures.append(fig)):
            result=fd.write_response_pair('unused_response',rows,'PM_A000_P75_PERFECT',
                result=dict(outcome='SOLVER_ERROR',pm_point=dict(frame=8,selection_kind='SIGNIFICANT_STIFFNESS_CHANGE',capacity_verified=False)))
        self.assertEqual(len(result['files']),2)
        axes=figures[0].axes;self.assertEqual(len(axes),2)
        for ax,key,y in ((axes[0],'shortening_mm','force_kN'),(axes[1],'rotation_mrad','moment_kNm')):
            curve=next(line for line in ax.lines if line.get_gid()=='response-pair-curve')
            self.assertEqual(list(curve.get_xdata()),[r[key] for r in rows])
            self.assertEqual(list(curve.get_ydata()),[r[y] for r in rows])
            labels=' '.join(label.get_text() for label in ax.get_xticklabels())
            self.assertIn('F0',labels);self.assertIn('F11',labels)
            self.assertTrue(all('\n' in label.get_text() for label in ax.get_xticklabels()))
        self.assertIn('SOLVER_ERROR',' '.join(t.get_text() for t in figures[0].texts))
        self.assertFalse(plt.get_fignums())

    def test_pair_handles_zero_moment_and_rejects_invalid_frame_mapping(self):
        from matplotlib.figure import Figure
        from unittest import mock
        rows=self.rows()
        for row in rows:row.update(moment_kNm=0.,rotation_mrad=0.)
        with mock.patch.object(Figure,'savefig',autospec=True):
            fd.write_response_pair('unused_zero',rows,'PM_P00_PERFECT')
        for key,value in (('frame',2),('rotation_mrad',float('nan'))):
            broken=self.rows();broken[-1][key]=value
            with self.assertRaises(ValueError):fd.write_response_pair('unused_bad',broken,'PM_BAD')


class ForceDisplacementTests(unittest.TestCase):
    def test_unwritten_lpf_history_reports_missing_converged_data(self):
        from types import SimpleNamespace as NS
        for data in (None, []):
            step = NS(historyRegions={'Assembly': NS(historyOutputs={
                'LPF': NS(data=data, description='Load proportionality factor')})})
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'LPF history'):
                fd.lpf_history(step)

    def test_duplicate_initial_zero_is_removed_before_partial_prefix(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        frames = [NS(frameValue=i, fieldOutputs={'U':object()}) for i in (0, 1, 2)]
        odb = NS(steps={'GMNIA':NS(frames=frames)}, rootAssembly=NS(nodeSets={
            'STEP5_BOTTOM':object(), 'STEP5_TOP':object()}))
        info = dict(reference_force_N_per_end=1000, settings=dict(reference_stress=240),
                    end_area_weights=[[['P1',1,1]],[['P1',2,1]]])
        with mock.patch.object(fd, 'lpf_history', return_value=[(0,0),(0,0),(1,.4),(2,.8),(3,.9)]), \
                mock.patch.object(fd, 'weighted_u3', return_value=0):
            rows, meta = fd.curve_rows(odb, 'GMNIA', info, allow_partial=True)
        self.assertEqual([r['lpf'] for r in rows], [0,.4,.8])
        self.assertEqual(meta['duplicate_initial_zero_samples'], 1)
        self.assertEqual(meta['dropped_history_samples'], 1)

    def test_end_response_rejects_local_nonfinite_and_duplicate_displacements(self):
        from types import SimpleNamespace as NS
        for method in (fd.weighted_u3, fd.coefficient_u3):
            for case in ('local', 'nonfinite', 'duplicate'):
                with self.subTest(method=method.__name__, case=case):
                    value = NS(instance=NS(name='P1'), nodeLabel=1,
                               precision='DOUBLE_PRECISION', dataDouble=(0., 0., 1.))
                    if case == 'local':
                        value.localCoordSystemDouble = ((0., 0., 1.), (0., 1., 0.), (1., 0., 0.))
                    elif case == 'nonfinite':
                        value.dataDouble = (0., 0., float('nan'))
                    field = NS(getSubset=lambda **kw: NS(values=[value]* (2 if case == 'duplicate' else 1)))
                    with self.assertRaises(ValueError):
                        method(field, object(), {('P1', 1): 1.})

    def test_live_snapshot_writes_four_outputs_without_changing_sources(self):
        import contextlib, io, json, os, tempfile
        from types import SimpleNamespace as NS
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            model='STEP5_PERFECT_FY240'
            cae=os.path.join(d,'step5.cae'); odb_path=os.path.join(d,model+'.odb')
            lock=os.path.join(d,model+'.lck')
            for path in (cae,odb_path,lock):
                with open(path,'w') as stream: stream.write('source unchanged')
            info={'settings':{'fy':240,'reference_stress':240,'field_frequency':1},
                  'end_area_mm2':4,'reference_force_N_per_end':1000,
                  'end_area_weights':[[['P1',1,1]],[['P1',2,1]]]}
            bottom=object(); top=object(); instance=NS(name='P1')
            def field(i):
                def subset(region):
                    label=1 if region is bottom else 2
                    return NS(values=[NS(instance=instance,nodeLabel=label,
                        precision='SINGLE_PRECISION',data=(0,0,0 if label==1 else -i))])
                return NS(getSubset=subset)
            frames=[NS(frameValue=i,fieldOutputs={'U':field(i)}) for i in range(3)]
            step=NS(frames=frames,historyRegions={'Assembly':NS(historyOutputs={
                'LPF':NS(data=[(0,0),(1,1),(2,.6)])})})
            odb=NS(steps={'GMNIA':step},rootAssembly=NS(nodeSets={
                'STEP5_BOTTOM':bottom,'STEP5_TOP':top}),close=mock.Mock())
            db=NS(models={model:NS(description=fd.STEP5_PREFIX+json.dumps(info))},close=mock.Mock())
            opener=mock.Mock(return_value=odb)
            args=NS(cae=cae,odb=odb_path,model=None,step='GMNIA',output_dir=d,allow_partial=True)
            modules={'caeModules':NS(),'abaqus':NS(openMdb=lambda **kw:db),
                     'odbAccess':NS(openOdb=opener)}
            with mock.patch.dict('sys.modules',modules), contextlib.redirect_stdout(io.StringIO()):
                summary=fd.extract(args)
            opener.assert_called_once_with(path=odb_path,readOnly=True)
            self.assertTrue(summary['partial'])
            self.assertEqual(summary['peak']['force_kN'],1)
            self.assertEqual(summary['peak']['shortening_mm'],1)
            for extension in ('csv','json','svg','png'):
                path=os.path.join(d,model+'_force_displacement_partial.'+extension)
                self.assertGreater(os.path.getsize(path),100)
            for path in (cae,odb_path,lock):
                with open(path) as stream: self.assertEqual(stream.read(),'source unchanged')
            db.close.assert_called_once(); odb.close.assert_called_once()

    def test_partial_flag_allows_readonly_locked_odb(self):
        import contextlib
        import io
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cae=os.path.join(d,'x.cae');odb=os.path.join(d,'x.odb')
            for path in (cae,odb,os.path.join(d,'x.lck')):
                with open(path,'w') as f: f.write('')
            args=fd.parse_arguments(['--cae',cae,'--odb',odb,'--allow-partial'])
            self.assertTrue(args.allow_partial)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                fd.parse_arguments(['--cae',cae,'--odb',odb])

    def test_live_history_ahead_of_fields_uses_synchronized_prefix(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        frames=[NS(frameValue=i,fieldOutputs={'U':object()}) for i in (0,1)]
        step=NS(frames=frames)
        odb=NS(steps={'GMNIA':step},rootAssembly=NS(nodeSets={'STEP5_BOTTOM':object(),'STEP5_TOP':object()}))
        info={'reference_force_N_per_end':1000,'settings':{'reference_stress':240},'end_area_weights':[[['P1',1,1]],[['P1',2,1]]]}
        with mock.patch.object(fd,'lpf_history',return_value=[(0,0),(1,1),(2,.8)]), mock.patch.object(fd,'weighted_u3',return_value=0):
            rows,meta=fd.curve_rows(odb,'GMNIA',info,allow_partial=True)
        self.assertEqual([r['lpf'] for r in rows],[0,1])
        self.assertTrue(meta['partial'])
        self.assertEqual(meta['dropped_history_samples'],1)

    def test_live_history_without_initial_sample_can_lead_at_equal_counts(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        frames=[NS(frameValue=i,fieldOutputs={'U':object()}) for i in (0,1,2)]
        odb=NS(steps={'GMNIA':NS(frames=frames)},rootAssembly=NS(nodeSets={
            'STEP5_BOTTOM':object(),'STEP5_TOP':object()}))
        info={'reference_force_N_per_end':1000,'settings':{'reference_stress':240},
              'end_area_weights':[[['P1',1,1]],[['P1',2,1]]]}
        with mock.patch.object(fd,'lpf_history',return_value=[(1,.4),(2,.8),(3,.9)]), mock.patch.object(fd,'weighted_u3',return_value=0):
            rows,meta=fd.curve_rows(odb,'GMNIA',info,allow_partial=True)
        self.assertEqual([r['lpf'] for r in rows],[0,.4,.8])
        self.assertTrue(meta['partial'])
        self.assertEqual(meta['dropped_history_samples'],1)

    def test_extractor_entry_runs_without_main_namespace_or_file(self):
        from pathlib import Path
        from unittest import mock
        entry=Path(fd.__file__).with_name('abaqus_step5_force_displacement.py')
        calls=[]
        with mock.patch.object(fd,'main',side_effect=lambda: calls.append(1)):
            exec(compile(entry.read_text(),str(entry),'exec'),{'__name__':'abaqus'})
        self.assertEqual(calls,[1])

    def test_weight_map_rejects_duplicate_and_nonpositive(self):
        self.assertEqual(fd.weight_map([['P1', 1, 2.0]]), {('P1', 1): 2.0})
        with self.assertRaises(ValueError):
            fd.weight_map([['P1', 1, 2.0], ['p1', 1, 3.0]])
        with self.assertRaises(ValueError):
            fd.weight_map([['P1', 1, 0.0]])

    def test_weighted_u3_survives_abaqus_sum_shadowing(self):
        import runpy
        from types import SimpleNamespace as NS

        def abaqus_sum(*args, **kwargs):
            raise TypeError("arg1; expecting a recognized Abaqus type")

        namespace = runpy.run_path(
            fd.__file__, init_globals={'sum': abaqus_sum},
            run_name='force_displacement_sum_shadow_test')
        instance = NS(name='P1')
        values = [
            NS(instance=instance, nodeLabel=1, precision='SINGLE_PRECISION', data=(0.0, 0.0, 2.0)),
            NS(instance=instance, nodeLabel=2, precision='SINGLE_PRECISION', data=(0.0, 0.0, 4.0)),
        ]
        field = NS(getSubset=lambda region: NS(values=values))
        weights = {('P1', 1): 1.0, ('P1', 2): 3.0}
        self.assertAlmostEqual(namespace['weighted_u3'](field, object(), weights), 3.5)

    def test_lpf_history_alignment_uses_sequence_when_abscissae_are_offset(self):
        from types import SimpleNamespace as NS
        frames = [NS(frameValue=0.0), NS(frameValue=0.10), NS(frameValue=0.20)]
        history = [(0.0, 0.0), (0.05, 0.75), (0.15, 1.10)]
        values, meta = fd.align_lpf_history(frames, history)
        self.assertEqual(values, [0.0, 0.75, 1.10])
        self.assertEqual(meta['mode'], 'same_count_by_sequence')
        self.assertAlmostEqual(meta['max_abs_frameValue_minus_historyX'], 0.05)

    def test_lpf_history_alignment_accepts_missing_initial_sample(self):
        from types import SimpleNamespace as NS
        frames = [NS(frameValue=0.0), NS(frameValue=0.10), NS(frameValue=0.20)]
        history = [(0.05, 0.75), (0.15, 1.10)]
        values, meta = fd.align_lpf_history(frames, history)
        self.assertEqual(values, [0.0, 0.75, 1.10])
        self.assertEqual(meta['mode'], 'synthesized_initial_zero_then_sequence')

    def test_equal_counts_cannot_assign_nonzero_load_to_initial_frame(self):
        from types import SimpleNamespace as NS
        with self.assertRaisesRegex(ValueError, 'initial'):
            fd.align_lpf_history([NS(frameValue=0.), NS(frameValue=.1)],
                                 [(.05, .75), (.15, 1.10)])

    def test_missing_final_history_sample_is_not_a_missing_initial_sample(self):
        from types import SimpleNamespace as NS
        with self.assertRaisesRegex(ValueError, 'initial'):
            fd.align_lpf_history([NS(frameValue=0.), NS(frameValue=.1), NS(frameValue=.2)],
                                 [(0., 0.), (.05, .75)])

    def test_lpf_history_alignment_rejects_ambiguous_count(self):
        from types import SimpleNamespace as NS
        frames = [NS(frameValue=0.0), NS(frameValue=0.10), NS(frameValue=0.20)]
        with self.assertRaises(ValueError):
            fd.align_lpf_history(frames, [(0.0, 0.0)])

    def test_partial_live_odb_uses_only_synchronized_prefix(self):
        from types import SimpleNamespace as NS

        class FakeField:
            def __init__(self, values):
                self.values = values
            def getSubset(self, region=None):
                return self

        instance = NS(name='P1')
        value = NS(instance=instance, nodeLabel=1, precision='SINGLE_PRECISION', data=(0.0, 0.0, 0.0))
        frames = []
        for i in range(6):
            frames.append(NS(frameValue=float(i), fieldOutputs={'U': FakeField([value])}))
        history = [(0.0, 0.0), (1.0, 0.1), (2.0, 0.2), (3.0, 0.3), (4.0, 0.4)]

        step = NS(frames=frames, historyRegions={})
        odb = NS(
            steps={'GMNIA': step},
            rootAssembly=NS(nodeSets={'STEP5_BOTTOM': object(), 'STEP5_TOP': object()}))

        original_lpf_history = fd.lpf_history
        original_weighted_u3 = fd.weighted_u3
        try:
            fd.lpf_history = lambda _step: history
            fd.weighted_u3 = lambda field, region, weights: 0.0
            info = {
                'end_area_weights': [[['P1', 1, 1.0]], [['P1', 1, 1.0]]],
                'reference_force_N_per_end': 1000.0,
                'settings': {'reference_stress': 240.0},
            }
            rows, meta = fd.curve_rows(odb, 'GMNIA', info, allow_partial=True)
            self.assertTrue(meta['partial'])
            self.assertEqual(meta['dropped_field_frames'], 1)
            self.assertEqual(len(rows), 5)
        finally:
            fd.lpf_history = original_lpf_history
            fd.weighted_u3 = original_weighted_u3

    def test_peak_summary_uses_maximum_force(self):
        rows = [
            dict(frame=0, lpf=0.0, force_N=0.0, force_kN=0.0, shortening_mm=0.0,
                 nominal_stress_MPa=0.0),
            dict(frame=1, lpf=1.2, force_N=12000.0, force_kN=12.0, shortening_mm=2.0,
                 nominal_stress_MPa=288.0),
            dict(frame=2, lpf=0.9, force_N=9000.0, force_kN=9.0, shortening_mm=4.0,
                 nominal_stress_MPa=216.0)]
        peak = fd.peak_summary(rows)
        self.assertEqual(peak['frame'], 1)
        self.assertAlmostEqual(peak['force_kN'], 12.0)
        self.assertAlmostEqual(peak['shortening_mm'], 2.0)

    def test_sparse_metadata_is_rejected_before_reading_odb(self):
        with self.assertRaisesRegex(ValueError, 'field_frequency'):
            fd.curve_rows(None,'GMNIA',{'settings':{'field_frequency':2}})

    def test_extra_nonzero_history_cannot_be_assigned_to_initial_frame(self):
        from types import SimpleNamespace as NS
        with self.assertRaisesRegex(ValueError, 'initial'):
            fd.align_lpf_history([NS(frameValue=0),NS(frameValue=2)],[(0,0),(1,1.2),(2,.7)])

    def test_ticks_are_finite_and_ordered(self):
        ticks = fd._nice_ticks(0.0, 13.0)
        self.assertTrue(ticks)
        self.assertTrue(all(math.isfinite(x) for x in ticks))
        self.assertEqual(ticks, sorted(ticks))

    def test_bending_moment_rotation_extraction(self):
        """Bending metadata -> M = LPF*M_ref and the work-conjugate end rotation sum k_i*U3_i."""
        import contextlib, io, json, os, tempfile
        from types import SimpleNamespace as NS
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            model='STEP5_PERFECT_FY240'
            cae=os.path.join(d,'step5.cae'); odb_path=os.path.join(d,model+'.odb')
            for path in (cae,odb_path):
                with open(path,'w') as stream: stream.write('source unchanged')
            # two loaded nodes per end at eta = +-100 mm, F = +-1 (unit), M_ref = 200 N mm per MPa
            weights=[[['P1',1,1/200.],['P1',2,-1/200.]],[['P1',3,-1/200.],['P1',4,1/200.]]]
            info={'settings':{'fy':240,'reference_stress':240,'field_frequency':1},
                  'load_case':{'type':'bending','neutral_axis_angle_deg':0.},
                  'reference_moment_Nmm':48000.,'first_yield_moment_Nmm':48000.,
                  'end_rotation_weights':weights,'bending_axis_deg':0.,
                  'section_for_PM':{'area_mm2':10.,'first_yield_moment_Nmm':48000.,'plastic_moment_Nmm':60000.},
                  'end_area_mm2':10.,'end_area_weights':[[['P1',1,5]],[['P1',3,5]]]}
            bottom=object(); top=object(); instance=NS(name='P1')
            eta={1:100.,2:-100.,3:100.,4:-100.}
            def field(i):
                beta=1e-3*i
                def subset(region):
                    labels=(1,2) if region is bottom else (3,4)
                    u3=lambda l: 0.5*i+(beta if l in (1,2) else -beta)*eta[l]
                    return NS(values=[NS(instance=instance,nodeLabel=l,precision='SINGLE_PRECISION',
                                         data=(0,0,u3(l))) for l in labels])
                return NS(getSubset=subset)
            frames=[NS(frameValue=i,fieldOutputs={'U':field(i)}) for i in range(3)]
            step=NS(frames=frames,historyRegions={'Assembly':NS(historyOutputs={
                'LPF':NS(data=[(0,0),(1,1),(2,.6)])})})
            odb=NS(steps={'GMNIA':step},rootAssembly=NS(nodeSets={
                'STEP5_BOTTOM':bottom,'STEP5_TOP':top}),close=mock.Mock())
            db=NS(models={model:NS(description=fd.STEP5_PREFIX+json.dumps(info))},close=mock.Mock())
            args=NS(cae=cae,odb=odb_path,model=None,step='GMNIA',output_dir=d,allow_partial=False,skip_png=True)
            modules={'caeModules':NS(),'abaqus':NS(openMdb=lambda **kw:db),
                     'odbAccess':NS(openOdb=mock.Mock(return_value=odb))}
            with mock.patch.dict('sys.modules',modules), contextlib.redirect_stdout(io.StringIO()):
                summary=fd.extract(args)
            self.assertAlmostEqual(summary['peak']['moment_kNm'],0.048)
            self.assertAlmostEqual(summary['peak']['rotation_mrad'],2.0)   # beta_b - beta_t = 2*beta
            self.assertEqual(summary['bending_axis_deg'],0.)
            for extension in ('csv','json','svg'):
                self.assertGreater(os.path.getsize(os.path.join(d,model+'_moment_rotation.'+extension)),100)
            with open(os.path.join(d,model+'_moment_rotation.svg')) as stream:
                self.assertIn('Bending moment, M (kN m)',stream.read())
        self.assertEqual(fd.response_kind({}), 'compression')
        with self.assertRaises(ValueError):
            fd.coefficient_map([['P1',1,0.0]])


class ReportReferencePlotTests(unittest.TestCase):
    def test_report_selection_and_legacy_normalization_are_independent(self):
        reference = dict(schema='cufsm_builtup_reference_v1', AsFy_kN=1100., axes={
            'x': dict(axis_deg=0., SFy_kNm=130., ZFy_kNm=190.),
            'z': dict(axis_deg=90., SFy_kNm=140., ZFy_kNm=200.)})
        info = dict(settings=dict(fy=240.), end_area_mm2=1000., first_yield_moment_Nmm=50e6,
                    load_case=dict(type='bending'), bending_axis_deg=270., report_reference=reference)
        lines = fd.capacity_lines(info)
        self.assertEqual([line['value'] for line in lines], [140., 200.])
        self.assertTrue(all('built-up z' in line['label'] for line in lines))
        info['bending_axis_deg'] = 45.
        self.assertTrue(all('report built-up x' in line['label'] for line in fd.capacity_lines(info)))
        info['load_case']['type'] = 'compression'
        self.assertEqual(fd.capacity_lines(info)[0]['value'], 1100.)
        info['load_case']['type'] = 'combined'
        self.assertEqual(fd.capacity_lines(info), [1.])
        info.pop('report_reference')
        info['load_case']['type'] = 'compression'
        self.assertEqual(fd.capacity_lines(info), [240.])
        info['load_case']['type'] = 'bending'
        self.assertEqual(fd.capacity_lines(info), [50.])

    def test_explicit_report_overlay_uses_model_fy_and_leaves_metadata_unchanged(self):
        from unittest import mock
        import abaqus_report_reference as reader
        info = dict(settings=dict(fy=360.), reference_force_N_per_end=42., report_reference={'old': True})
        with mock.patch.object(reader, 'read_reference', return_value={'new': True}) as read:
            updated = fd.with_report_reference(info, 'report-folder')
        read.assert_called_once_with('report-folder', fy=360.)
        self.assertEqual(updated['reference_force_N_per_end'], 42.)
        self.assertEqual(updated['report_reference'], {'new': True})
        self.assertEqual(info['report_reference'], {'old': True})

    def test_mixed_legacy_and_labelled_capacity_lines_are_supported(self):
        axes = fd.RESPONSE_AXES['bending']
        self.assertEqual(fd.normalize_capacity_lines([12., {'value': 18., 'label': 'ZFy report'}], axes),
                         [{'value': 12., 'label': 'My = 12.000 kN m'},
                          {'value': 18., 'label': 'ZFy report'}])
        with self.assertRaisesRegex(ValueError, 'finite'):
            fd.normalize_capacity_lines([float('nan')], axes)

    def test_individual_svg_includes_both_report_benchmarks(self):
        import os, tempfile
        import xml.etree.ElementTree as ET
        axes = fd.RESPONSE_AXES['bending']
        rows = [dict(rotation_mrad=0., moment_kNm=0.), dict(rotation_mrad=2., moment_kNm=3.)]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'curve.svg')
            fd.write_svg(path, rows, rows[-1], 'Bending', axes,
                         yield_lines=[{'value': 5., 'label': 'SFy report x'},
                                      {'value': 7., 'label': 'ZFy report x'}])
            root = ET.parse(path).getroot()
            texts = [node.text for node in root.iter('{http://www.w3.org/2000/svg}text')]
            self.assertIn('SFy report x', texts)
            self.assertIn('ZFy report x', texts)
            refs = [node for node in root.iter('{http://www.w3.org/2000/svg}line')
                    if node.get('stroke-dasharray')]
            self.assertEqual(len(refs), 2)
            self.assertTrue(all(node.get('y1') == node.get('y2') for node in refs))


if __name__ == '__main__':
    unittest.main()
