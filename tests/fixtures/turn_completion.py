"""Wait for the exact terminal notification without hydrating unfinished history."""
from test_background_integration import wait


def wait_for_turn_completion(rpc, parent, ident):
    def completed():
        # Metadata reads pump notifications before first-turn history is materialized.
        rpc.call('thread/read', {'threadId': parent})
        turn = next((message['params']['turn'] for message in rpc.messages
            if message.get('method') == 'turn/completed'
            and message['params']['threadId'] == parent
            and message['params']['turn']['id'] == ident), None)
        if turn:
            assert turn['status'] == 'completed', turn
            return turn

    return wait(completed, f'exact turn {ident} did not complete')
