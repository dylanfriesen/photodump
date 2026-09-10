import unittest
from unittest.mock import AsyncMock, patch

from app import progress, worker


class ProgressTests(unittest.TestCase):
    def setUp(self):
        with patch.object(progress, 'typical_seconds', return_value=None):
            progress.start(6, 'wan_i2v')
        self.addCleanup(progress.done)
        self.graph = {
            'first': {'class_type': 'SamplerCustomAdvanced', 'inputs': {}},
            'middle': {'class_type': 'LTXVLatentUpsampler', 'inputs': {'samples': ['first', 0]}},
            'second': {'class_type': 'SamplerCustomAdvanced', 'inputs': {'latent_image': ['middle', 0]}},
            'decode': {'class_type': 'VAEDecodeTiled', 'inputs': {'samples': ['second', 0]}},
        }

    def test_unknown_progress_stays_unknown(self):
        p = progress.snapshot()
        self.assertIsNone(p['percent'])
        self.assertIsNone(p['eta'])

    def test_two_pass_progress_does_not_reset(self):
        handle = worker._progress_handler(self.graph, ['ours'])
        handle('progress', {'prompt_id': 'ours', 'node': 'first', 'value': 8, 'max': 8})
        self.assertEqual(progress.snapshot()['percent'], 46)
        handle('progress', {'prompt_id': 'ours', 'node': 'second', 'value': 1, 'max': 4})
        self.assertEqual(progress.snapshot()['percent'], 57.5)
        handle('executing', {'prompt_id': 'ours', 'node': 'decode'})
        self.assertEqual(progress.snapshot()['percent'], 96)

    def test_reconnect_during_second_pass_uses_graph(self):
        handle = worker._progress_handler(self.graph, ['ours'])
        handle('progress', {'prompt_id': 'ours', 'node': 'second', 'value': 2, 'max': 4})
        self.assertEqual(progress.snapshot()['percent'], 69)
        self.assertEqual(progress.snapshot()['stage'], 'sampling pass 2/2')

    def test_other_prompt_cannot_move_current_bar(self):
        handle = worker._progress_handler(self.graph, ['ours'])
        handle('progress', {'prompt_id': 'theirs', 'node': 'first', 'value': 8, 'max': 8})
        self.assertIsNone(progress.snapshot()['percent'])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_prompt_does_not_rebuild_or_resubmit(self):
        from contextlib import asynccontextmanager
        clients = []
        @asynccontextmanager
        async def socket(client, handler):
            clients.append(client)
            yield
        graph = {'3': {'class_type': 'KSampler', 'inputs': {}}}
        with patch.object(worker.comfy, 'pending_prompt', AsyncMock(return_value={'graph': graph, 'client_id': 'original'})), \
             patch.object(worker.comfy, 'progress_socket', socket), \
             patch.object(worker.comfy, 'build') as build, \
             patch.object(worker.comfy, 'submit', AsyncMock()) as submit, \
             patch.object(worker, '_await_outputs', AsyncMock()) as collect:
            await worker._run({'id': 6, 'prompt_id': 'existing'})
            self.assertEqual(clients, ['original'])
            build.assert_not_called()
            submit.assert_not_called()
            self.assertEqual(collect.call_args.args[0], 'existing')


if __name__ == '__main__':
    unittest.main()
