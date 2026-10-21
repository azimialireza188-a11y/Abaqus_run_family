"""Compare orchestration checks using fake child processes, never Abaqus."""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import abaqus_compare_pipeline as compare


class ComparePipelineTests(unittest.TestCase):
    def test_robust_controls_and_no_predictor_reach_all_three_variants(self):
        args=self.arguments(['--include-hss','--cufsm-report-dir',str(self.root/'report'),
                             '--riks-controls','robust','--riks-extrapolation','none',
                             '--pm-directory','PM_robust_no_predictor'])
        requests=compare.build_requests(args)
        self.assertEqual([r['variant'] for r in requests],['no_plates','with_plates','hss'])
        for request in requests:
            flags=request['arguments']
            for flag,value in (('--riks-controls','robust'),('--riks-extrapolation','none'),
                               ('--pm-directory','PM_robust_no_predictor')):
                self.assertEqual(flags[flags.index(flag)+1],value)

    def setUp(self):
        self.root = Path(__file__).resolve().parent/('compare_test_'+uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def arguments(self, extra=()):
        return compare.parse_arguments(['--family-root', str(self.root/'families'),
            '--output-root', str(self.root/'out'), '--steel-plate-length', '40',
            '--steel-plate-length-2', '40', '--steel-plate-skip-count', '0,3',
            '--global-modes', '1', '--model-cases', 'perfect', *extra])

    def fake_plan(self, request):
        if request['variant']=='hss':
            return [dict(pm_dir=str(request['output_root']/'HSS_Axis000'/'Step5_PM_HSS'),
                         label='HSS',axis_deg=0.)]
        folder=request['output_root']/'families'/('family_1_'+request['load_case'])/'imperfection'/'PM'
        return [dict(family='family_1', pm_dir=str(folder), skip_count=None)]

    def test_common_settings_are_shared_and_plate_flags_never_reach_plain_code(self):
        args=self.arguments(['--load-case','compression','--load-case','bending',
                             '--bending-axis-deg','45','--mesh-mm','12','--steel-column-plate-fy','350'])
        requests=compare.build_requests(args)
        self.assertEqual(len(requests),4)
        for request in requests:
            command=request['arguments']
            self.assertEqual(command[command.index('--mesh-mm')+1],'12.0')
            self.assertEqual(command[command.index('--steel-column-plate-fy')+1],'350.0')
            self.assertIn('--pm-interaction',command)
            if request['variant']=='no_plates':
                self.assertNotIn('--steel-plate-length',command)
                self.assertNotIn('--steel-plate-fy',command)
            else:self.assertIn('--steel-plate-skip-count',command)
            if request['load_case']=='compression':self.assertNotIn('--bending-axis-deg',command)
        self.assertEqual(len({r['output_root'] for r in requests}),2)

    def test_csv_load_cases_and_single_variant_are_supported(self):
        args=self.arguments(['--load-cases','compression,bending','--variants','no-plates'])
        requests=compare.build_requests(args)
        self.assertEqual([r['load_case'] for r in requests],['compression','bending'])
        self.assertTrue(all(r['variant']=='no_plates' for r in requests))

    def test_pm_only_and_parallel_budget_reach_each_independent_pipeline(self):
        args=self.arguments(['--pm-only','--parallel-jobs','2','--cpus','12'])
        for request in compare.build_requests(args):
            flags=request['arguments']
            self.assertIn('--pm-only',flags)
            self.assertEqual(flags[flags.index('--parallel-jobs')+1],'2')
            self.assertEqual(flags[flags.index('--cpus')+1],'12')

    def test_comparison_parallel_workers_reject_invalid_counts(self):
        for value in ('0','-2','1.5','bad'):
            with self.subTest(value=value),self.assertRaises(SystemExit):
                self.arguments(['--parallel-jobs',value])

    def test_hss_is_an_independent_optional_dataset_with_its_own_case_selection(self):
        args=self.arguments(['--include-hss','--hss-model-cases','both',
                             '--cufsm-report-dir',str(self.root/'report'),'--pm-only'])
        requests=compare.build_requests(args)
        self.assertEqual([r['variant'] for r in requests],['no_plates','with_plates','hss'])
        hss=requests[-1]
        self.assertEqual(hss['script'],'abaqus_hss_pipeline.py')
        self.assertEqual(hss['output_root'],args.output_root/'hss')
        flags=hss['arguments']
        self.assertEqual(flags[flags.index('--model-cases')+1],'both')
        self.assertNotIn('--steel-plate-length',flags)
        self.assertNotIn('--pm-only',flags)  # HSS entry point inherently runs P-M only.

    def test_hss_does_not_require_report_and_never_forwards_None_as_a_path(self):
        args=self.arguments(['--include-hss','--hss-model-cases','perfect'])
        requests=compare.build_requests(args)
        self.assertEqual(len(requests),3)
        for request in requests:
            self.assertNotIn('--cufsm-report-dir',request['arguments'])
            self.assertNotIn('None',request['arguments'])

    def test_demand_csv_is_validated_before_solves_and_forwarded_only_to_collector(self):
        path=self.root/'demands.csv'
        path.write_text('id,floor,column,load_case,P_kN,M_major_kNm,M_minor_kNm,notes\n'
                        'G-I-1,1,interior,gravity,1804.4236,9.80665,,approximate\n')
        args=self.arguments(['--demand-csv',str(path),'--dry-run'])
        for request in compare.build_requests(args):
            self.assertNotIn('--demand-csv',request['arguments'])
        with mock.patch('builtins.print') as printed:
            compare.run_comparison(args,probe=self.fake_plan,runner=lambda *a,**k:self.fail('solver launched'))
        self.assertTrue(any('--demand-csv' in str(call) for call in printed.call_args_list))
        self.assertFalse(args.output_root.exists())
        path.write_text('broken CSV')
        with self.assertRaisesRegex(ValueError,'Demand CSV'):
            compare.run_comparison(args,probe=lambda *a:self.fail('invalid demand reached model preflight'))

    def test_hss_perfect_selection_is_forwarded_independently_of_builtup_cases(self):
        args=self.arguments(['--include-hss','--hss-model-cases','perfect',
                             '--model-cases','both','--cufsm-report-dir',str(self.root/'report'),
                             '--pm-only','--resume'])
        requests=compare.build_requests(args)
        for request in requests:
            flags=request['arguments']
            expected='perfect' if request['variant']=='hss' else 'both'
            self.assertEqual(flags[flags.index('--model-cases')+1],expected)
            self.assertIn('--resume',flags)

    def test_every_child_preflight_finishes_before_any_execution_or_output_write(self):
        args=self.arguments()
        calls=[]
        def probe(request):
            calls.append(request['variant'])
            if request['variant']=='with_plates':raise ValueError('invalid plated settings')
            return self.fake_plan(request)
        with self.assertRaisesRegex(ValueError,'invalid plated'):
            compare.run_comparison(args,probe=probe,runner=lambda *a,**k:self.fail('premature execution'))
        self.assertEqual(calls,['no_plates','with_plates'])
        self.assertFalse(args.output_root.exists())

    def test_broken_plotting_dependency_fails_before_any_pipeline(self):
        args=self.arguments()
        original=compare.importlib.import_module
        def broken_import(name,*a,**kw):
            if name=='matplotlib.pyplot':raise ImportError('broken plotting dependency')
            return original(name,*a,**kw)
        with mock.patch.object(compare.importlib,'import_module',side_effect=broken_import):
            with self.assertRaisesRegex(RuntimeError,'Matplotlib'):
                compare.run_comparison(args,probe=lambda r:self.fail('plotting not validated first'))
        self.assertFalse(args.output_root.exists())

    def test_dry_run_does_not_create_outputs_or_launch_solvers(self):
        args=self.arguments(['--dry-run'])
        compare.run_comparison(args,probe=self.fake_plan,runner=lambda *a,**k:self.fail('dry run launched child'))
        self.assertFalse(args.output_root.exists())

    def test_combined_execution_collects_only_planned_roots_with_distinct_labels(self):
        args=self.arguments(['--resume'])
        requests=compare.build_requests(args)
        calls=[]
        def runner(command,**kwargs):
            calls.append(command)
            if '--mode' not in command:
                live = json.loads(kwargs['env']['STEP5_LIVE_REPORT_CONTEXT'])
                self.assertEqual(live['output_dir'],str(args.output_root/'diagram'))
                self.assertEqual(len(live['datasets']),2)
                request=next(r for r in requests if str(r['repo']/'abaqus_family_pipeline.py') in command)
                for plan in self.fake_plan(request):
                    folder=Path(plan['pm_dir']);folder.mkdir(parents=True)
                    (folder/'STEP5_PM_points.csv').write_text('model,P_kN,M_kNm\nPM,1,2\n')
            else:
                output=Path(command[command.index('--output-dir')+1]);output.mkdir(parents=True,exist_ok=True)
                for name in ('PM_interaction.png','PM_interaction.svg','PM_interaction_points.csv'):
                    (output/name).write_text('fake comparison')
            return SimpleNamespace(returncode=0)
        compare.run_comparison(args,probe=self.fake_plan,runner=runner)
        self.assertEqual(len(calls),3)
        collect=calls[-1]
        start,end=collect.index('--roots')+1,collect.index('--dataset-labels')
        self.assertEqual(set(collect[start:end]),{p['pm_dir'] for r in requests for p in self.fake_plan(r)})
        labels=collect[end+1:collect.index('--output-dir')]
        self.assertEqual(len(labels),2)
        self.assertNotEqual(*labels)
        manifest=json.loads((args.output_root/'comparison_manifest.json').read_text())
        self.assertEqual(manifest['status'],'COMPLETED')

    def test_collect_only_never_launches_a_model_pipeline(self):
        args=self.arguments(['--collect-only'])
        for request in compare.build_requests(args):
            for plan in self.fake_plan(request):
                folder=Path(plan['pm_dir']);folder.mkdir(parents=True)
                (folder/'STEP5_PM_points.csv').write_text('model,P_kN,M_kNm\nPM,1,2\n')
        def runner(command,**kwargs):
            self.assertIn('--mode',command)
            output=Path(command[command.index('--output-dir')+1]);output.mkdir(parents=True,exist_ok=True)
            for name in ('PM_interaction.png','PM_interaction.svg','PM_interaction_points.csv'):
                (output/name).write_text('fake comparison')
        compare.run_comparison(args,probe=self.fake_plan,runner=runner)

    def test_child_failure_stops_before_combining_results(self):
        import subprocess
        args=self.arguments()
        calls=[]
        def runner(command,**kwargs):
            calls.append(command)
            raise subprocess.CalledProcessError(1,command)
        with self.assertRaises(subprocess.CalledProcessError):
            compare.run_comparison(args,probe=self.fake_plan,runner=runner)
        self.assertEqual(len(calls),1)
        self.assertEqual(json.loads((args.output_root/'comparison_manifest.json').read_text())['status'],'FAILED')

    def test_hss_and_both_builtup_datasets_are_collected_together(self):
        args=self.arguments(['--include-hss','--cufsm-report-dir',str(self.root/'report'),
                             '--pm-only','--parallel-jobs','2'])
        requests=compare.build_requests(args)
        calls=[]
        def runner(command,**kwargs):
            calls.append(command)
            if '--mode' in command:
                out=Path(command[command.index('--output-dir')+1]);out.mkdir(parents=True,exist_ok=True)
                for name in ('PM_interaction_points.csv','PM_interaction.png','PM_interaction.svg'):
                    (out/name).write_text('mock comparison')
            else:
                request=requests[len(calls)-1]
                for item in self.fake_plan(request):
                    out=Path(item['pm_dir']);out.mkdir(parents=True,exist_ok=True)
                    (out/'STEP5_PM_points.csv').write_text('model,P_kN,M_kNm\nPM,1,2\n')
        compare.run_comparison(args,probe=self.fake_plan,runner=runner)
        self.assertEqual(len(calls),4)
        self.assertTrue(calls[2][2].endswith('abaqus_hss_pipeline.py'))
        collect=calls[-1]
        roots=collect[collect.index('--roots')+1:collect.index('--dataset-labels')]
        labels=collect[collect.index('--dataset-labels')+1:collect.index('--output-dir')]
        self.assertEqual(len(roots),3)
        self.assertEqual(len(set(labels)),3)
        self.assertTrue(any(label.startswith('HSS') for label in labels))


if __name__=='__main__':
    unittest.main()
