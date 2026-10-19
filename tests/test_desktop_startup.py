"""入口行为回归：仅执行 AST 中的纯启动逻辑，不导入应用、不访问任何库。"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

TREE = ast.parse((Path(__file__).parents[1] / 'server/main.py').read_text(encoding='utf-8'))


class DesktopStartupTests(unittest.TestCase):
    def test_default_ports_and_sandbox_override(self):
        nodes = [n for n in TREE.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in {'EDITION', 'PORT'} for t in n.targets)]
        for frozen, override, expected in [(False, '', 8765), (True, '', 8766), (True, '8816', 8816)]:
            scope = {'sys': SimpleNamespace(frozen=frozen),
                     'os': SimpleNamespace(environ={'FAPIAO_PORT': override})}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), 'ports', 'exec'), scope)
            self.assertEqual(scope['PORT'], expected)

    def check_open(self, started, should_exit, becomes_ready):
        opened = []
        server = SimpleNamespace(started=started, should_exit=should_exit)
        class Thread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        scope = {
            'threading': SimpleNamespace(Thread=Thread),
            'webbrowser': SimpleNamespace(open=opened.append),
            'time': SimpleNamespace(sleep=lambda _: setattr(server, 'started', becomes_ready)),
        }
        fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == '_open_browser_when_ready')
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'startup', 'exec'), scope)
        scope['_open_browser_when_ready']('http://127.0.0.1:8816/', server)
        return opened

    def test_only_opens_ready_server(self):
        self.assertEqual(self.check_open(False, False, True), ['http://127.0.0.1:8816/'])

    def test_failed_or_unready_server_does_not_open_other_instance(self):
        self.assertEqual(self.check_open(False, True, False), [])
        self.assertEqual(self.check_open(False, False, False), [])


if __name__ == '__main__':
    unittest.main()
