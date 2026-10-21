"""Small geometry regressions; no Abaqus kernel, solver or classification."""
import os
from pathlib import Path
import shutil
import unittest
import uuid

import numpy as np

from gdlc_classifier import abaqus_mfsm_export as exporter
from gdlc_classifier import gdlc_mode_decomposition as gdlc


class PlateContactGeometryTests(unittest.TestCase):
    def setUp(self):
        # Default inherited Windows ACLs also work with sandboxed Python 3.14.
        self.root = Path(__file__).resolve().parent/('gdlc_geometry_test_'+uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def fixture(self, station=0., adjustment=3e-7, contact_evidence=True):
        lines = ['*Part, name=WALL', '*Node']
        corners = [(-10., -10.), (10., -10.), (10., 10.), (-10., 10.)]
        for r, z in enumerate((0., 20.)):
            lines += [f'{r*4+i+1}, {x}, {y}, {z}' for i, (x, y) in enumerate(corners)]
        lines += ['*Element, type=S4R', '1,1,2,6,5', '2,2,3,7,6',
                  '3,3,4,8,7', '4,4,1,5,8', '*End Part',
                  '*Part, name=PLATE', '*Node', '1,0,-10,0', '2,10,0,0',
                  '3,0,10,0', '4,-10,0,0', '*Element, type=S4R',
                  '1,1,2,3,4', '*End Part', '*Assembly, name=A',
                  '*Instance, name=P1, part=WALL', f'0,0,{station}', '*End Instance',
                  '*Instance, name=STEEL_PLATE_001, part=PLATE', f'0,0,{station+5}',
                  '*End Instance', '*End Assembly']
        inp = self.root/'job.inp'
        inp.write_text('\n'.join(lines)+'\n')
        nodes = exporter.read_input(inp)['nodes']
        inst = np.array([k[0] for k in nodes])
        labels = np.array([k[1] for k in nodes])
        expected = np.array(list(nodes.values()))
        xyz = expected.astype(np.float32).astype(float)
        plate = np.flatnonzero(inst == 'STEEL_PLATE_001')
        xyz[plate[0], 0] = float(np.float32(adjustment))
        if contact_evidence:
            self.write_evidence(inp)
        return inp, inst, labels, expected, xyz, plate

    def write_evidence(self, inp):
        inp.with_suffix('.dat').write_text(
            ' SECONDARY: General_Contact_Faces\n MAIN: General_Contact_Faces\n'
            '              DISTANCE\n NODE ADJUSTED REMARKS/WARNINGS\n'
            ' 9 3.00000E-07 ADJUSTED TO (3.00000E-07, -10., 5.).\n'
            ' NOT ALL THE NODES THAT HAVE BEEN ADJUSTED WERE PRINTED.\n')

    def test_bounded_contact_difference_retains_actual_plate_coordinates(self):
        inp, inst, labels, expected, xyz, plate = self.fixture()
        geometry, provenance = gdlc.verified_input_coordinates(inst, labels, xyz, inp)
        np.testing.assert_array_equal(geometry[plate], xyz[plate])
        np.testing.assert_array_equal(geometry[:8], expected[:8])
        self.assertEqual(provenance['plate_adjusted_node_count'], 1)
        self.assertEqual(provenance['source'], 'inp_member_and_modal_export_plates')
        self.assertFalse(provenance['plate_contact_adjustment_evidence']['nodewise_verified'])

    def test_missing_contact_evidence_cannot_relax_plate_validation(self):
        inp, inst, labels, _, xyz, _ = self.fixture(contact_evidence=False)
        with self.assertRaisesRegex(ValueError, '(?i)(coordinates|contact)'):
            gdlc.verified_input_coordinates(inst, labels, xyz, inp)

    def test_stale_or_unrelated_dat_is_not_contact_evidence(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                inp, inst, labels, _, xyz, _ = self.fixture()
                dat = inp.with_suffix('.dat')
                if stale:
                    os.utime(dat, (1, 1))
                else:
                    dat.write_text('Unrelated output: 9 3e-7 ADJUSTED TO (0,0,0).')
                with self.assertRaisesRegex(ValueError, '(?i)(coordinates|contact)'):
                    gdlc.verified_input_coordinates(inst, labels, xyz, inp)

    def test_member_difference_remains_strict_even_with_contact_evidence(self):
        inp, inst, labels, _, xyz, _ = self.fixture()
        xyz[0, 0] += 1e-6
        with self.assertRaisesRegex(ValueError, '(?i)coordinates'):
            gdlc.verified_input_coordinates(inst, labels, xyz, inp)

    def test_plate_bound_does_not_grow_with_global_station(self):
        for station in (0., 1e8):
            with self.subTest(station=station):
                inp, inst, labels, _, xyz, _ = self.fixture(station=station, adjustment=1e-4)
                with self.assertRaisesRegex(ValueError, '(?i)coordinates'):
                    gdlc.verified_input_coordinates(inst, labels, xyz, inp)

    def test_valid_fp32_rounding_at_large_station_is_not_a_contact_difference(self):
        inp, inst, labels, _, xyz, plate = self.fixture(station=1e8, adjustment=1e-6)
        geometry, provenance = gdlc.verified_input_coordinates(inst, labels, xyz, inp)
        np.testing.assert_array_equal(geometry[plate], xyz[plate])
        self.assertEqual(provenance['plate_adjusted_node_count'], 1)

    def test_transverse_plate_normals_do_not_change_member_axis_or_rings(self):
        inp, inst, labels, _, xyz, plate = self.fixture()
        # Scramble the export order: identities, rather than archive order, bind geometry.
        order = np.arange(len(labels))[::-1]
        np.savez(self.root/'raw_dof_map.npz', node_instances=inst[order],
                 node_labels=labels[order], node_coordinates=xyz[order])
        mesh = gdlc.load_mesh(self.root, dict(shards=['modes_0001.npz']), inp)
        np.testing.assert_array_equal(mesh.axis, [0., 0., 1.])
        np.testing.assert_array_equal(mesh.ring_z, [0., 20.])
        np.testing.assert_array_equal(mesh.xyz, gdlc.verified_input_coordinates(
            inst[order], labels[order], xyz[order], inp)[0])
        self.assertTrue(np.all(mesh.ring_of[np.char.startswith(mesh.instance, 'STEEL_PLATE_')] == -1))
        self.assertEqual(len(mesh.elems), 5)  # Plate elements remain available for plate metrics.

    def test_plate_rounding_without_adjustment_preserves_export_geometry(self):
        inp, inst, labels, _, xyz, plate = self.fixture(station=1e8, adjustment=0., contact_evidence=False)
        geometry, _ = gdlc.verified_input_coordinates(inst, labels, xyz, inp)
        np.testing.assert_array_equal(geometry[plate], xyz[plate])


if __name__ == '__main__':
    unittest.main()
