import math
import csv
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import abaqus_hss_pipeline as hss


class HSSModelTests(unittest.TestCase):
    def test_no_extrapolation_uses_the_same_native_keyword_as_cae_models(self):
        cfg=self.config(); mesh=hss.make_mesh(cfg,400.)
        pattern=hss.loading(mesh,cfg,0.,75.)
        for profile in ('default','robust'):
            text=hss.input_text(mesh,cfg,pattern,nonlinear=True,extrapolation='none',riks_controls=profile)
            self.assertIn('extrapolation=NO\n',text)
            self.assertNotIn('extrapolation=NONE',text)
        with self.assertRaisesRegex(ValueError,'extrapolation'):
            hss.input_text(mesh,cfg,pattern,nonlinear=True,extrapolation='parabolic')

    def test_robust_controls_are_written_without_changing_the_perfect_mesh_or_material(self):
        cfg = self.config(); mesh = hss.make_mesh(cfg, 400.)
        pattern = hss.loading(mesh, cfg, 0., 30.)
        original = hss.input_text(mesh, cfg, pattern, nonlinear=True)
        robust = hss.input_text(mesh, cfg, pattern, nonlinear=True, riks_controls='robust')
        self.assertEqual(original[:original.index('*Step')], robust[:robust.index('*Step')])
        self.assertIn('*Controls, analysis=DISCONTINUOUS', robust)
        self.assertIn('*Controls, parameters=LINE SEARCH\n4, 1, 0.0001, 0.25, 0.1', robust)
        self.assertNotIn('parameters=FIELD', robust)
        self.assertIn('inc=5000', robust)
        self.assertIn('0.01, 1., 1.e-8, 0.05', robust)

    def test_report_free_hss_preserves_nominal_area_radius_and_read_only_preflight(self):
        from test_report_reference import ModelReferenceTests
        root=self.fixture(); family=root/'family_1'; family.mkdir()
        ModelReferenceTests().family(family)
        args=hss.parse_arguments(['--family-root',str(root),'--output-root',str(root/'out'),
                                  '--fy','240','--model-cases','perfect'])
        with mock.patch.object(hss,'read_hss_report',side_effect=AssertionError('Excel opened')):
            cfg=hss.preflight(args)['geometry']
        self.assertFalse((root/'out').exists())
        self.assertAlmostEqual(cfg['radius_mm'],5.)
        self.assertAlmostEqual(cfg['B_mm'],160./8.+(2.-math.pi/2.)*5.+2.)
        self.assertAlmostEqual(cfg['area_report_mm2'],160.)
        from abaqus_hss_aisc import rounded_properties
        self.assertAlmostEqual(rounded_properties(cfg['B_mm'],cfg['H_mm'],2.,5.)['area_mm2'],160.)

    def test_no_report_resume_keeps_old_input_provenance_and_rejects_real_geometry_change(self):
        from test_report_reference import ModelReferenceTests
        root=self.fixture(); family=root/'family_1'; family.mkdir()
        ModelReferenceTests().family(family)
        args=hss.parse_arguments(['--family-root',str(root),'--output-root',str(root/'out'),
                                  '--fy','240','--model-cases','perfect','--resume'])
        plan=hss.preflight(args)
        # Emulate an existing run whose workbook is no longer available.
        cfg=plan['geometry']; cfg.pop('source'); cfg['source_workbook']='deleted.xlsx'
        cfg['area_report_mm2']=159.899116739
        request=hss.pipeline_request(args,plan)
        path=Path(args.output_root)/'hss_pipeline.json'; path.parent.mkdir()
        path.write_text(json.dumps(request)); before=path.read_bytes()
        with mock.patch.object(hss,'read_hss_report',side_effect=AssertionError('Excel opened')):
            current=hss.preflight(args)
        self.assertEqual(current['geometry'],cfg)
        self.assertEqual(path.read_bytes(),before)
        args.hss_radius_mm=6.
        with self.assertRaisesRegex(ValueError,'geometry/material differs'):
            hss.preflight(args)

    def test_missing_radius_requires_explicit_value_instead_of_guessing(self):
        from test_report_reference import ModelReferenceTests
        root=self.fixture(); ModelReferenceTests().family(root)
        path=root/'builtup_member.csv'; path.write_text(path.read_text().replace('_R2p5',''))
        with self.assertRaisesRegex(ValueError,'--hss-radius-mm'):
            hss.hss_from_family(root,240.)
        self.assertEqual(hss.hss_from_family(root,240.,radius=5.)['radius_mm'],5.)

    def config(self):
        return dict(B_mm=502.1460183660255, H_mm=502.1460183660255,
                    t_mm=2., radius_mm=5., length_mm=3600., E_MPa=200000., nu=.3,
                    fy_MPa=240., area_report_mm2=3983.8991167391205)

    def fixture(self):
        root = Path(__file__).resolve().parent/('hss_test_'+uuid.uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        return root

    def test_closed_shell_has_no_duplicate_seam_and_keeps_exact_end_and_middle_rings(self):
        mesh = hss.make_mesh(self.config(), 20.)
        self.assertEqual(mesh['z'][0], 0.)
        self.assertEqual(mesh['z'][-1], 3600.)
        self.assertIn(1800., mesh['z'])
        self.assertEqual(len(mesh['elements']), len(mesh['section'])*(len(mesh['z'])-1))
        for ring in (0, len(mesh['z'])-1):
            self.assertEqual(len({tuple(p) for p in mesh['section']}), len(mesh['section']))
        last = mesh['elements'][len(mesh['section'])-1]
        self.assertEqual(last[1], 1)
        self.assertEqual(last[2], len(mesh['section'])+1)

    def test_end_tractions_have_balanced_resultants_and_pure_endpoints(self):
        cfg = self.config(); mesh = hss.make_mesh(cfg, 20.)
        pure_p = hss.loading(mesh, cfg, 0., 0.)
        pure_m = hss.loading(mesh, cfg, 0., 90.)
        self.assertAlmostEqual(pure_p['moment_Nmm'], 0.)
        self.assertAlmostEqual(pure_m['axial_force_N'], 0.)
        for load in (pure_p, pure_m):
            self.assertAlmostEqual(sum(load['bottom'].values())+sum(load['top'].values()), 0., places=7)
        self.assertAlmostEqual(pure_p['axial_force_N'], mesh['area_mm2']*240.)

    def test_supports_leave_end_warping_free_and_have_only_one_middle_axial_anchor(self):
        cfg = self.config(); mesh = hss.make_mesh(cfg, 100.)
        text = hss.input_text(mesh, cfg, hss.loading(mesh, cfg, 0., 45.), nonlinear=True)
        self.assertIn('ENDS, 1, 2, 0.', text)
        self.assertIn('ENDS, 6, 6, 0.', text)
        self.assertIn('AXIAL_ANCHOR, 3, 3, 0.', text)
        self.assertNotIn('ENDS, 3', text)
        self.assertNotIn('*MPC', text)
        self.assertIn('*Static, riks', text)
        self.assertNotIn('*Equation', text)

    def test_reference_selection_refuses_missing_or_impure_own_modes(self):
        row = dict(mode='1', eigenvalue='12.', rel_G_pct='0.', rel_D_pct='0.',
                   rel_L_pct='99.', rel_C_pct='0.', rel_O_pct='1.',
                   G_pct='0.', D_pct='0.', L_pct='99.', C_pct='0.', O_pct='1.')
        self.assertEqual(hss.validate_mode([row], 1, 'L')['mode'], '1')
        with self.assertRaises(ValueError):
            hss.validate_mode([row], 2, 'L')
        with self.assertRaises(ValueError):
            hss.validate_mode([dict(row, L_pct='90.', O_pct='10.')], 1, 'L')
        with self.assertRaises(ValueError):
            hss.validate_mode([dict(row, L_pct='nan')], 1, 'L')

    def test_imperfections_use_normalized_own_modes_and_preserve_supports(self):
        import numpy as np
        cfg = self.config(); mesh = hss.make_mesh(cfg, 200.)
        xyz = np.array(mesh['nodes'])
        l = np.zeros_like(xyz); g = np.zeros_like(xyz)
        for i, p in enumerate(xyz):
            envelope = math.sin(math.pi*p[2]/3600.)
            l[i, :2] = np.array(mesh['normals'][i % len(mesh['section'])])*envelope
            g[i, 1] = envelope
        moved, provenance = hss.imperfect_coordinates(mesh, cfg, l, g, 0.)
        self.assertAlmostEqual(provenance['a_L_mm'], -.68)
        self.assertAlmostEqual(provenance['a_G_mm'], 3.6)
        for ring in (0, len(mesh['z'])-1):
            start = ring*len(mesh['section'])
            self.assertTrue(np.allclose(moved[start:start+len(mesh['section'])], xyz[start:start+len(mesh['section'])]))
        with self.assertRaises(ValueError):
            hss.imperfect_coordinates(mesh, cfg, np.zeros_like(l), g, 0.)

    def test_preflight_is_read_only_and_rejects_ambiguous_family_elastic_properties(self):
        import abaqus_complete_model_m20 as builder
        root = self.fixture()
        for number in (1, 2):
            family = root/('family_'+str(number)); family.mkdir()
            (family/'builtup_segments.csv').write_text('')
            (family/'builtup_member.csv').write_text('L_mm\n3600\n')
        output = root/'not_created'
        args = hss.parse_arguments(['--family-root', str(root), '--cufsm-report-dir', str(root),
                                   '--output-root', str(output), '--fy', '240', '--model-cases', 'imperfect',
                                   '--pm-directory', 'Step5_PM_ROBUST'])
        self.assertEqual(args.model_cases, 'imperfection')
        with mock.patch.object(hss, 'read_hss_report', return_value=self.config()), \
                mock.patch.object(builder, 'read_geometry', return_value=({}, 2., 200000., .3)):
            plan = hss.preflight(args)
        self.assertFalse(output.exists())
        self.assertEqual(len(plan['pm_dirs']), 1)
        self.assertEqual(plan['geometry']['E_MPa'], 200000.)
        self.assertEqual(Path(plan['pm_dirs'][0]['pm_dir']).name, 'Step5_PM_ROBUST')
        with mock.patch.object(hss, 'read_hss_report', return_value=self.config()), \
                mock.patch.object(builder, 'read_geometry', side_effect=[({}, 2., 200000., .3), ({}, 2., 210000., .3)]):
            with self.assertRaisesRegex(ValueError, 'different E/nu'):
                hss.preflight(args)

    def test_alternate_pm_directory_preserves_shared_provenance_and_refuses_changed_geometry(self):
        import copy
        root = self.fixture(); path = root/'hss_pipeline.json'
        original = dict(plan=dict(geometry=dict(t_mm=2.), pm_dirs=[dict(pm_dir='old')]), mesh_mm=20.,
                        riks_extrapolation='linear')
        path.write_text(json.dumps(original)); before = path.read_bytes()
        changed = copy.deepcopy(original)
        changed['plan']['pm_dirs'][0]['pm_dir'] = 'new'
        changed['riks_extrapolation'] = 'none'
        hss.preserve_pipeline_plan(path, changed)
        self.assertEqual(path.read_bytes(), before)
        args=SimpleNamespace(riks_extrapolation='none',riks_controls='robust')
        self.assertEqual(hss.validate_riks_settings(args,None),'robust')
        with self.assertRaisesRegex(ValueError,'extrapolation'):
            hss.validate_riks_settings(args,dict(riks_extrapolation='linear',riks_controls='robust'))
        self.assertEqual(hss.validate_riks_settings(args,dict(riks_extrapolation='none',riks_controls='robust')),
                         'robust')
        changed['plan']['geometry']['t_mm'] = 3.
        with self.assertRaisesRegex(ValueError, 'differs'):
            hss.preserve_pipeline_plan(path, changed)
        self.assertEqual(path.read_bytes(), before)

    def test_perfect_inputs_keep_own_hss_metadata_and_pass_shared_queue_validation(self):
        import abaqus_step5_queue as queue
        import abaqus_step5_force_displacement_core as fd
        root = self.fixture(); cfg = self.config(); mesh = hss.make_mesh(cfg, 200.)
        args = SimpleNamespace(model_cases='perfect', fy=240., global_modes=[1],
                               pm_angles=[0., 90.], riks_extrapolation='linear', mesh_mm=200.)
        jobs = hss.prepare_points(args, root, mesh, cfg, 0.)
        self.assertEqual(len(jobs), 2)
        for name in jobs:
            info = json.loads((root/(name+'_step5.json')).read_text())
            self.assertTrue(queue.validate_submission(str(root), name, info, 'monitor', .70))
            self.assertNotIn('report_reference', info)
            self.assertEqual(info['hss_report_reference']['B_mm'], cfg['B_mm'])
            self.assertEqual(info['source_step4']['case']['local_component_mm'], 0.)
            self.assertEqual(fd.response_kind(info), 'combined')
            for weights in info['end_area_weights']:
                self.assertAlmostEqual(sum(fd.weight_map(weights).values()), mesh['area_mm2'])
            for weights in info['end_rotation_weights']:
                self.assertTrue(fd.coefficient_map(weights))

    def test_export_artifact_hashes_are_required_and_tampering_is_rejected(self):
        root = self.fixture()
        for name in ('raw_dof_map.npz', 'modes_0001.npz'):
            (root/name).write_bytes(b'original fixture')
        metadata = dict(source_stability_verified=True, shards=['modes_0001.npz'],
                        artifact_sha256={name: hss.sha256(root/name) for name in ('raw_dof_map.npz', 'modes_0001.npz')})
        hss.verify_export_artifacts(root, metadata)
        (root/'modes_0001.npz').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            hss.verify_export_artifacts(root, metadata)

    def test_perfect_workflow_preserves_nominal_nodes_saves_cae_and_queues_only_perfect_jobs(self):
        root=self.fixture(); output=root/'output'; cfg=self.config()
        args=hss.parse_arguments(['--family-root',str(root),'--cufsm-report-dir',str(root),
            '--output-root',str(output),'--fy','240','--model-cases','perfect',
            '--axes','0','--mesh-mm','500','--pm-angles','0,15,30,45,60,75,90','--resume'])
        directory=output/'HSS_Axis000'/'Step5_PM_HSS'
        plan=dict(geometry=cfg,pm_dirs=[dict(pm_dir=str(directory),axis_deg=0.)])
        mesh=hss.make_mesh(cfg,args.mesh_mm)
        queued=[]
        def save_cae(unused,directory,files,destination):
            self.assertEqual(destination.name,'HSS_PM.cae')
            self.assertEqual(len(files),7)
            self.assertTrue(all(path.stem.endswith('_PERFECT') for path in files))
            destination.write_text('mock CAE preservation')
        def solve(unused,command,cwd):
            jobs=command[command.index('--jobs')+1:command.index('--cpus')]
            queued.extend(jobs)
            self.assertEqual(len(jobs),7)
            self.assertIn('--resume',command)
            for name in jobs:
                self.assertTrue(name.endswith('_PERFECT'))
                text=(directory/(name+'.inp')).read_text()
                nodes=text.split('*Node\n',1)[1].split('*Element',1)[0]
                xyz=[tuple(float(v.strip()) for v in row.split(',')[1:]) for row in nodes.splitlines()]
                self.assertEqual(xyz,[tuple(point) for point in mesh['nodes']])
                info=json.loads((directory/(name+'_step5.json')).read_text())
                case=info['source_step4']['case']
                self.assertEqual(case['label'],'PERFECT')
                self.assertTrue(all(case[key]==0. for key in ('a_L_mm','a_D_mm','a_G_mm')))
            (directory/'STEP5_PM_points.csv').write_text('model,P_kN,M_kNm\n')
        with mock.patch.object(hss,'preflight',return_value=plan), \
                mock.patch.object(hss,'own_modes',side_effect=AssertionError('Perfect must not solve reference modes')), \
                mock.patch.object(hss,'imperfect_coordinates',side_effect=AssertionError('Perfect must not apply imperfection')), \
                mock.patch.object(hss,'save_cae',side_effect=save_cae), \
                mock.patch.object(hss,'command',side_effect=solve):
            self.assertEqual(hss.run(args),plan)
        self.assertEqual(len(queued),7)
        self.assertTrue((directory/'HSS_PM.cae').is_file())
        self.assertFalse((output/'reference').exists())


    def test_cae_adapter_sets_imported_model_metadata_through_supported_api(self):
        import sys
        root=self.fixture()
        source=root/'PM_P00_PERFECT.inp';source.write_text('*Heading\n')
        (root/'PM_P00_PERFECT_step5.json').write_text('{"stage": 5}')
        class ImportedModel:
            __slots__=('saved',)
            def setValues(self,**values):self.saved=values
        model=ImportedModel()
        database=SimpleNamespace(ModelFromInputFile=mock.Mock(return_value=model),
                                 Job=mock.Mock(),saveAs=mock.Mock())
        with mock.patch.dict(sys.modules,{'abaqus':SimpleNamespace(mdb=database)}):
            hss.native_save(['--native-save-cae',str(root/'HSS_PM.cae'),
                             '--native-inputs',str(source)])
        self.assertTrue(model.saved['description'].startswith('STEP5_GMNIA '))
        database.Job.assert_called_once_with(name='PM_P00_PERFECT',model='PM_P00_PERFECT')

    def test_hss_native_entry_accepts_kernel_flags_without_separator_and_keeps_user_flags_strict(self):
        import sys
        root=self.fixture()
        destination=str(root/'HSS_PM.cae'); source=str(root/'PM_P00_PERFECT.inp')
        kernel=['-cae','-noGUI',str(Path(hss.__file__).with_name('abaqus_hss_nogui.py')),
                '-tmpdir',str(root/'kernel temp'),'-lmlog','ON']
        user=['--native-save-cae',destination,'--native-inputs',source]
        for argv in (kernel+user,user+kernel,kernel+['--']+user,kernel+['--']+user+kernel):
            database=mock.Mock()
            with self.subTest(argv=argv),mock.patch.dict(sys.modules,{'abaqus':SimpleNamespace(mdb=database)}):
                hss.main(argv)
            database.ModelFromInputFile.assert_called_once_with(name='PM_P00_PERFECT',inputFileName=source)
            database.saveAs.assert_called_once_with(pathName=str(Path(destination).with_suffix('')))
        with mock.patch.dict(sys.modules,{'abaqus':SimpleNamespace(mdb=mock.Mock())}):
            with self.assertRaises(SystemExit):
                hss.main(kernel+user+['--misspelled-user-option'])

    def test_hss_cae_save_reports_replay_reason_when_launcher_succeeds_without_database(self):
        root=self.fixture(); source=root/'PM_P00_PERFECT.inp'; source.write_text('*Heading\n')
        (root/'abaqus.rpy').write_text('#: error: unrecognized arguments: -cae -noGUI\n')
        args=SimpleNamespace(abaqus_cmd='abaqus')
        with mock.patch.object(hss,'command'):
            with self.assertRaisesRegex(RuntimeError,'unrecognized arguments: -cae -noGUI'):
                hss.save_cae(args,root,[source],root/'HSS_PM.cae')

    def test_hss_cae_save_keeps_native_log_and_reuses_only_nonempty_cae(self):
        root=self.fixture();source=root/'PM_P00_PERFECT.inp';source.write_text('*Heading\n')
        destination=root/'HSS_PM.cae';args=SimpleNamespace(abaqus_cmd='abaqus')
        def native(command,**kwargs):
            kwargs['stdout'].write('mock CAE save diagnostic\n')
            destination.write_bytes(b'mock nonempty database')
            return 0
        with mock.patch.object(hss.subprocess,'call',side_effect=native) as launch:
            hss.save_cae(args,root,[source],destination)
            hss.save_cae(args,root,[source],destination)
        launch.assert_called_once()
        self.assertIn('mock CAE save diagnostic',(root/'HSS_CAE_save.log').read_text())
        destination.write_bytes(b'')
        with mock.patch.object(hss.subprocess,'call') as launch:
            with self.assertRaisesRegex(ValueError,'empty.*preserved'):
                hss.save_cae(args,root,[source],destination)
        launch.assert_not_called()

    def test_hss_nogui_driver_reports_argparse_failure_even_in_foreign_namespace(self):
        driver=Path(hss.__file__).with_name('abaqus_hss_nogui.py')
        for code in (0,2):
            with self.subTest(code=code),mock.patch.object(hss,'main',side_effect=SystemExit(code)):
                namespace={'__name__':'abaqus'}
                if code:
                    with self.assertRaisesRegex(RuntimeError,'argument parsing failed'):
                        exec(compile(driver.read_text(),str(driver),'exec'),namespace)
                else:
                    exec(compile(driver.read_text(),str(driver),'exec'),namespace)


class AISCReferenceTests(unittest.TestCase):
    def test_rounded_properties_match_independent_strip_integration(self):
        import numpy as np
        from abaqus_hss_aisc import rounded_properties
        B, H, t, R = 310., 310., 4., 10.
        result = rounded_properties(B, H, t, R)
        # Integrate horizontal material strips, independently of corner moments.
        z, w = np.polynomial.legendre.leggauss(160)
        def solid(B, H, r):
            total = np.zeros(3)
            for lo, hi in ((0., H/2-r), (H/2-r, H/2)):
                y = lo+(z+1)*(hi-lo)/2
                widths = np.full_like(y, B) if hi <= H/2-r else B-2*r+2*np.sqrt(r*r-(y-(H/2-r))**2)
                total += np.array([np.dot(w,widths),np.dot(w,widths*y*y),np.dot(w,widths*y)])*(hi-lo)
            return total
        values = solid(B,H,R+t/2)-solid(B-2*t,H-2*t,R-t/2)
        for key, value in zip(('area_mm2','Ix_mm4','Zx_mm3'),values):
            self.assertAlmostEqual(result[key]/value,1.,places=6)

    def test_E7_effective_width_matches_AISC_manual_example(self):
        from abaqus_hss_aisc import effective_width
        self.assertAlmostEqual(effective_width(11.5,.174,29000.,50.,29.1),8.66,delta=.03)

    def test_slender_web_and_flange_has_no_fabricated_F7_strength(self):
        from abaqus_hss_aisc import nominal_reference
        cfg = HSSModelTests().config()
        result = nominal_reference(cfg)
        self.assertIsNone(result['Mn_kNm'])
        self.assertEqual(result['flexure_status'],'OUTSIDE_F7_SLENDER_WEB_AND_FLANGE')
        self.assertGreater(result['Pn_kN'],0.)
        self.assertLess(result['Pn_kN'],result['Py_kN'])

    def test_compact_square_uses_rounded_plastic_modulus_and_explicit_model_thickness(self):
        from abaqus_hss_aisc import nominal_reference
        cfg = dict(B_mm=120.,H_mm=120.,t_mm=6.,radius_mm=12.,length_mm=3600.,E_MPa=200000.,fy_MPa=240.)
        result = nominal_reference(cfg)
        self.assertEqual(result['flexure_status'],'F7_SECTION_REFERENCE')
        self.assertAlmostEqual(result['Mn_kNm'],result['Mp_kNm'])
        self.assertEqual(result['thickness_basis'],'actual Abaqus model wall; no product thickness reduction inferred')
        self.assertLessEqual(result['Pn_kN'],result['P0_effective_kN'])

    def test_convexity_check_does_not_move_or_fill_in_points(self):
        import copy
        from abaqus_hss_aisc import convexity_diagnostic
        good=[(0.,100.),(50.,60.),(100.,0.)]
        bad=[(0.,100.),(50.,20.),(100.,0.)]
        original=copy.deepcopy(bad)
        self.assertEqual(convexity_diagnostic(good)['status'],'CONVEX_SAMPLES')
        self.assertEqual(convexity_diagnostic(bad)['status'],'NONCONVEX_SAMPLES')
        self.assertEqual(bad,original)
        self.assertEqual(convexity_diagnostic([(0.,100.),(50.,60.)])['status'],'INCOMPLETE_ENDPOINTS')

    def test_references_reach_primary_demand_queue_and_normalized_plots_without_mutating_points(self):
        import copy
        from matplotlib.figure import Figure
        import abaqus_pm_demands as demand
        import abaqus_pm_interaction as pm
        root=HSSModelTests.fixture(self)
        cfg=dict(B_mm=310.,H_mm=310.,t_mm=4.,radius_mm=10.,length_mm=3600.,E_MPa=200000.,fy_MPa=240.)
        (root/'hss_manifest.json').write_text(json.dumps(dict(geometry=cfg,
            end_condition='both ends U1 U2 UR3=0; one middle U3 anchor; free end warping')))
        points=[dict(folder=str(root),source='GMNIA',source_case='PERFECT',dataset_label='HSS',
            bending_axis_deg=0.,pm_angle_deg=a,P_kN=p,M_kNm=m,Py_kN=1000.,My_kNm=100.,
            selection_kind='VERIFIED_PEAK',capacity_verified=True)
            for a,p,m in ((0.,900.,0.),(45.,400.,40.),(90.,0.,90.))]
        # Different specimens use their own Py/My in normalized comparisons.
        points += [dict(points[1],folder=str(root/'plain'),dataset_label='No plates',
                        Py_kN=1100.,My_kNm=75.)]
        original=copy.deepcopy(points);captured={}
        def capture(fig,path,**kw):
            if str(path).endswith('.png'):captured[Path(path).name]=fig
        demands=[dict(id='1',floor=1,column='interior',load_case='gravity',P_kN=200.,M_major_kNm=20.,M_minor_kNm=None)]
        with mock.patch.object(Figure,'savefig',autospec=True,side_effect=capture):
            demand.write_readable_plots(root,points,{},demands=demands,publish_primary=True)
            pm.plot_interaction(root,points,{},normalized=True)
            pm.plot_interaction(root,points,{})
        for name in ('01_PM_models_readable.png','02_PM_demands_readable.png','PM_interaction_normalized.png','PM_interaction.png'):
            axes=[ax for ax in captured[name].axes if ax.get_xlabel()]
            for ax in axes:
                self.assertTrue(any(line.get_gid()=='hss-aisc-pn' for line in ax.lines),name)
                self.assertTrue(any(line.get_gid()=='hss-aisc-mn' for line in ax.lines),name)
        physical=next(line for line in captured['PM_interaction.png'].axes[0].lines if line.get_gid()=='hss-aisc-mn')
        normalized=next(line for line in captured['PM_interaction_normalized.png'].axes[0].lines if line.get_gid()=='hss-aisc-mn')
        self.assertAlmostEqual(physical.get_xdata()[0]/100.,normalized.get_xdata()[0])
        self.assertEqual(points,original)
        self.assertTrue((root/'PM_convexity_diagnostics.json').is_file())
        self.assertTrue((root/'HSS_AISC360_nominal_reference.json').is_file())


if __name__ == '__main__':
    unittest.main()
