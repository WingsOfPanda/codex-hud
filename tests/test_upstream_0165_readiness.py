"""First-turn readiness keeps history errors fatal after exact completion."""
import unittest

from fixtures.turn_completion import wait_for_turn_completion
from codex_plus.rpc import RpcError


def notification(parent='parent', ident='turn', status='completed'):
    return {'method': 'turn/completed', 'params': {
        'threadId': parent, 'turn': {'id': ident, 'status': status}}}


class PendingHistory:
    def __init__(self, events=None, history_error=None, metadata_error=None):
        self.messages = []
        self.events = events if events is not None else [[notification()]]
        self.history_error = history_error
        self.metadata_error = metadata_error
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if params.get('includeTurns'):
            if not any(event == notification() for event in self.messages):
                # Same RPC response as list_turns with missing first-turn metadata.
                raise RpcError(method, {'code': -32601, 'message': 'list_turns is not supported yet'})
            if self.history_error:
                raise self.history_error
            return {'thread': {'turns': [{'id': 'turn', 'status': 'completed'}]}}
        if self.metadata_error:
            raise self.metadata_error
        if self.events:
            self.messages.extend(self.events.pop(0))
        return {'thread': {'id': 'parent'}}


class TurnReadinessTests(unittest.TestCase):
    def test_missing_metadata_before_completion_then_strict_history_read(self):
        rpc = PendingHistory(events=[[], [notification()]])
        with self.assertRaisesRegex(RpcError, 'list_turns is not supported yet'):
            rpc.call('thread/read', {'threadId': 'parent', 'includeTurns': True})
        wait_for_turn_completion(rpc, 'parent', 'turn')
        thread = rpc.call('thread/read', {'threadId': 'parent', 'includeTurns': True})['thread']
        self.assertEqual(thread['turns'], [{'id': 'turn', 'status': 'completed'}])
        self.assertEqual([params.get('includeTurns', False) for _, params in rpc.calls],
                         [True, False, False, True])

    def test_only_exact_thread_and_turn_complete(self):
        rpc = PendingHistory(events=[[notification(parent='other'), notification(ident='other')],
                                     [notification()]])
        self.assertEqual(wait_for_turn_completion(rpc, 'parent', 'turn')['id'], 'turn')
        self.assertEqual(len(rpc.calls), 2)

    def test_failed_and_interrupted_completion_fail(self):
        for status in ('failed', 'interrupted'):
            with self.subTest(status=status), self.assertRaises(AssertionError):
                wait_for_turn_completion(PendingHistory(events=[[notification(status=status)]]),
                                         'parent', 'turn')

    def test_metadata_errors_propagate(self):
        error = RpcError('thread/read', {'code': -32603, 'message': 'database read failed'})
        rpc = PendingHistory(metadata_error=error)
        with self.assertRaises(RpcError) as raised:
            wait_for_turn_completion(rpc, 'parent', 'turn')
        self.assertIs(raised.exception, error)
        self.assertEqual(len(rpc.calls), 1)

    def test_persistent_unsupported_and_other_history_errors_fail(self):
        for code, message in ((-32601, 'list_turns is not supported yet'),
                              (-32603, 'database read failed')):
            error = RpcError('thread/read', {'code': code, 'message': message})
            rpc = PendingHistory(history_error=error)
            wait_for_turn_completion(rpc, 'parent', 'turn')
            with self.subTest(code=code), self.assertRaises(RpcError) as raised:
                rpc.call('thread/read', {'threadId': 'parent', 'includeTurns': True})
            self.assertIs(raised.exception, error)
            self.assertEqual(len(rpc.calls), 2)


if __name__ == '__main__':
    unittest.main()
