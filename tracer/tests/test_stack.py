from code_tracer import stack as stackmod

PY = '''Traceback (most recent call last):
  File "app.py", line 8, in main
    return process(data)
  File "pkg/service.py", line 6, in process
    return transform(data)
ValueError: bad
'''

PHP = '''PHP Fatal error:  Uncaught Exception: boom in /var/www/x.php:15
Stack trace:
#0 /var/www/app.php(15): process()
#1 /var/www/index.php(3): main()
#2 {main}
  thrown in /var/www/x.php on line 15
'''

GO = '''panic: runtime error: index out of range

goroutine 1 [running]:
main.process(0xc000010200)
\t/app/service.go:6 +0x1d
main.main()
\t/app/main.go:8 +0x2f
'''

NODE = '''Error: boom
    at process (pkg/service.py:6:10)
    at main (app.py:8:5)
    at Object.<anonymous> (/app/index.js:1:1)
'''


def test_detect_and_parse_python():
    fmt, frames = stackmod.parse(PY)
    assert fmt == "python"
    assert len(frames) == 2
    assert frames[0].func == "main" and frames[0].line == 8


def test_detect_and_parse_php():
    fmt, frames = stackmod.parse(PHP)
    assert fmt == "php"
    assert any(f.func == "process" for f in frames)
    assert any(f.line == 15 for f in frames)


def test_detect_and_parse_go():
    fmt, frames = stackmod.parse(GO)
    assert fmt == "go"
    assert len(frames) == 2
    assert frames[0].file.endswith("service.go") and frames[0].line == 6
    assert frames[0].func == "process"


def test_detect_and_parse_node():
    fmt, frames = stackmod.parse(NODE)
    assert fmt == "node"
    assert len(frames) == 3
    assert frames[0].func == "process" and frames[0].line == 6


def test_bind_marks_out_of_index(indexed):
    db, _, _ = indexed
    fmt, frames = stackmod.parse(PY)
    frames = stackmod.bind(db, frames)
    assert frames[0].in_index is True   # app.py:main is indexed
    assert frames[0].symbol_id is not None
    # a bogus frame outside the index
    fmt2, f2 = stackmod.parse(
        'Traceback (most recent call last):\n'
        '  File "/usr/lib/python3.11/os.py", line 200, in nope\n    x\n'
    )
    f2 = stackmod.bind(db, f2)
    assert f2[0].in_index is False
