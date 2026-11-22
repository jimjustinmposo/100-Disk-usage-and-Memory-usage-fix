import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

source = Path(__file__).resolve().parent / 'memory_optimizer.py'
if not source.exists(): source = Path(__file__).resolve().parents[1] / 'outputs/MemoryOptimizer/memory_optimizer.py'
spec = importlib.util.spec_from_file_location('optimizer', source)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(m, 'JOURNAL', Path(self.tmp.name) / 'rollback.json')
        self.p.start()
        self.admin = patch.object(m.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=True)
        self.admin.start()
    def tearDown(self):
        self.admin.stop()
        self.p.stop()
        self.tmp.cleanup()
    def test_restore_failure_blocks_all_changes(self):
        with patch.object(m, 'settings', return_value={'compression': False, 'pageAuto': False}), patch.object(m, 'restore_point', side_effect=RuntimeError('No restore point')), patch.object(m, 'powershell') as command:
            with self.assertRaisesRegex(RuntimeError, 'No restore point'): m.optimize({})
            command.assert_not_called()
            self.assertFalse(m.JOURNAL.exists())
    def test_unelevated_blocks_restore_and_changes(self):
        with patch.object(m.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=False), patch.object(m, 'restore_point') as restore:
            with self.assertRaisesRegex(RuntimeError, 'administrator'): m.optimize({})
            restore.assert_not_called()
    def test_partial_failure_keeps_write_ahead_record(self):
        with patch.object(m, 'settings', side_effect=[{'compression':False,'pageAuto':False}, {'compression':False,'pageAuto':False}, {'compression':True,'pageAuto':False}]), patch.object(m, 'restore_point', return_value={'sequence':1}), patch.object(m, 'powershell', side_effect=[None, RuntimeError('paging failure')]):
            with self.assertRaisesRegex(RuntimeError, 'paging failure'): m.optimize({})
        j = m.json.loads(m.JOURNAL.read_text())
        self.assertEqual(j['status'], 'partial_failure')
        self.assertEqual(j['changes'][0]['status'], 'applied')
        self.assertEqual(j['changes'][1]['status'], 'pending')
    def test_rollback_handles_pending_and_verified_entries(self):
        m.save_json(m.JOURNAL, {'changes':[{'key':'compression','before':False,'applied':True,'status':'applied'}, {'key':'pageAuto','before':False,'applied':True,'status':'pending'}], 'status':'partial_failure'})
        with patch.object(m, 'settings', side_effect=[{'pageAuto':False}, {'compression':True}, {'compression':False}]), patch.object(m, 'powershell') as command:
            m.rollback()
            self.assertEqual(command.call_count, 1)
            self.assertIn('Disable-MMAgent', command.call_args.args[0])
        self.assertEqual(m.json.loads(m.JOURNAL.read_text())['status'], 'rolled_back')
    def test_active_journal_blocks_second_run(self):
        m.save_json(m.JOURNAL, {'changes':[{'key':'compression'}], 'status':'in_progress'})
        with patch.object(m, 'settings') as state:
            with self.assertRaisesRegex(RuntimeError, 'still active'): m.optimize({})
            state.assert_not_called()
    def test_healthy_settings_are_not_written(self):
        with patch.object(m, 'settings', return_value={'compression':True,'pageAuto':True}), patch.object(m, 'restore_point', return_value={'sequence':2}), patch.object(m, 'powershell') as command:
            actions, _ = m.optimize({})
            self.assertEqual(actions, [])
            command.assert_not_called()
    def test_growth_needs_time_and_substantial_growth(self):
        self.assertFalse(m.suspected_growth([(0,1),(400,1024**3)]))
        points = [(i*40, 300*1024**2+i*40*1024**2) for i in range(10)]
        self.assertTrue(m.suspected_growth(points))
        self.assertFalse(m.suspected_growth([(i, b) for i, (_,b) in enumerate(points)]))
    def test_native_metrics(self):
        p=m.performance()
        self.assertGreater(p['CommitLimit'], 0)
        self.assertGreater(p['CommitTotal'], 0)
    def test_pressure_is_bounded(self):
        self.assertEqual(m.pressure(95,99,1000),100)
        self.assertEqual(m.pressure(20,30,None),30)
    def test_manual_pagefile_entries_are_restored(self):
        original=['C:\\pagefile.sys 2048 8192']
        m.save_json(m.JOURNAL, {'before':{'pagingFiles':original}, 'changes':[{'key':'pageAuto','before':False,'applied':True,'status':'applied'}], 'status':'complete'})
        with patch.object(m, 'settings', side_effect=[{'pageAuto':True,'pagingFiles':['C:\\pagefile.sys 0 0']}, {'pageAuto':False,'pagingFiles':original}, {'pageAuto':False,'pagingFiles':original}]), patch.object(m, 'powershell') as command:
            m.rollback()
            self.assertEqual(command.call_count, 2)
            self.assertIn('MultiString', command.call_args.args[0])

if __name__ == '__main__': unittest.main()
