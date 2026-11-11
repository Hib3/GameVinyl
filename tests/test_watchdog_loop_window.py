import importlib.util
import os
import subprocess
from pathlib import Path

def test_watchdog_tick_prefers_windowless_python(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[1]/'watchdog_loop.py'
    spec=importlib.util.spec_from_file_location('watchdog_loop_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    scripts=tmp_path/'.venv'/'Scripts';scripts.mkdir(parents=True)
    (scripts/'python.exe').touch()
    if os.name=='nt': (scripts/'pythonw.exe').touch()
    monkeypatch.setattr(mod,'ROOT',tmp_path)
    calls=[]
    monkeypatch.setattr(mod.subprocess,'run',lambda *a,**kw:calls.append((a,kw)))
    mod.tick()
    assert len(calls)==1
    command,kwargs=calls[0]
    assert Path(command[0][0]).name==('pythonw.exe' if os.name=='nt' else 'python.exe')
    assert kwargs['creationflags']==(subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if os.name=='nt':
        (scripts/'pythonw.exe').unlink()
        calls.clear();mod.tick()
        assert Path(calls[0][0][0][0]).name=='python.exe'
        assert calls[0][1]['creationflags']==subprocess.CREATE_NO_WINDOW

def test_startup_fallback_starts_watchdog_and_bot(tmp_path, monkeypatch):
    source=Path(__file__).resolve().parents[1]/'setup_tasks.py'
    spec=importlib.util.spec_from_file_location('setup_tasks_watchdog_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    python=tmp_path/'pythonw.exe';python.touch()
    monkeypatch.setattr(mod,'ROOT',tmp_path)
    monkeypatch.setattr(mod,'VENV_PYW',python)
    monkeypatch.setattr(mod,'START_REQUEST_FILE',tmp_path/'data'/'logs'/'start_requested_at.txt')
    monkeypatch.setattr(mod,'read_mode',lambda:'startup-folder')
    monkeypatch.setattr(mod,'log',lambda *args:None)
    monkeypatch.setattr(mod,'flush_log',lambda:None)
    calls=[]
    monkeypatch.setattr(mod.subprocess,'Popen',lambda *args,**kwargs:calls.append((args,kwargs)))
    assert mod.start_now()==0
    assert [Path(call[0][0][1]).name for call in calls]==['task_launcher.py']*4
    assert [call[0][0][5] for call in calls]==[
        'run.py','collector/server.py','site/freshness_loop.py','watchdog_loop.py']

def test_scheduler_start_skips_hourly_site_task(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[1]/'setup_tasks.py'
    spec=importlib.util.spec_from_file_location('setup_tasks_scheduler_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    monkeypatch.setattr(mod,'START_REQUEST_FILE',tmp_path/'start.txt')
    monkeypatch.setattr(mod,'read_mode',lambda:'task-scheduler')
    monkeypatch.setattr(mod,'log',lambda *args:None)
    monkeypatch.setattr(mod,'flush_log',lambda:None)
    calls=[]
    monkeypatch.setattr(mod,'run_cmd',lambda args:(calls.append(args) or 0,None))
    assert mod.start_now()==0
    assert calls==[['schtasks','/Run','/TN',mod.MAIN_TASK],['schtasks','/Run','/TN',mod.COLLECTOR_TASK]]

def test_watchdog_loop_single_instance_across_processes(tmp_path, monkeypatch):
    import shutil
    import sys
    import time
    root=Path(__file__).resolve().parents[1]
    (tmp_path/'src'/'grt').mkdir(parents=True)
    (tmp_path/'src'/'grt'/'__init__.py').write_text('',encoding='utf-8')
    shutil.copyfile(root/'src'/'grt'/'singleinstance.py',tmp_path/'src'/'grt'/'singleinstance.py')
    shutil.copyfile(root/'watchdog_loop.py',tmp_path/'watchdog_loop.py')
    marker=tmp_path/'ticks.txt'
    (tmp_path/'watchdog.py').write_text("from pathlib import Path\np=Path(__file__).with_name('ticks.txt')\nwith p.open('a',encoding='utf-8') as f: f.write('tick\\n')\n",encoding='utf-8')
    monkeypatch.setenv('GRT_DATA_DIR',str(tmp_path/'data'))
    exe=getattr(sys,'_base_executable',sys.executable)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    args=[exe,str(tmp_path/'watchdog_loop.py')]
    first=subprocess.Popen(args,cwd=tmp_path,stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,creationflags=flags)
    try:
        deadline=time.monotonic()+12
        lockfile=tmp_path/'data'/'watchdog_loop.lock'
        while not lockfile.exists() and first.poll() is None and time.monotonic()<deadline:
            time.sleep(0.1)
        assert lockfile.exists() and first.poll() is None
        time.sleep(0.3)
        second=subprocess.run(args,cwd=tmp_path,stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,creationflags=flags,timeout=8)
        assert second.returncode==0
        assert first.poll() is None
        assert not marker.exists()
    finally:
        first.terminate()
        first.wait(timeout=8)


def test_watchdog_waits_for_main_boot_before_first_tick(tmp_path, monkeypatch):
    import pytest
    from grt import singleinstance
    source=Path(__file__).resolve().parents[1]/'watchdog_loop.py'
    spec=importlib.util.spec_from_file_location('watchdog_delay_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    monkeypatch.setattr(mod,'ROOT',tmp_path)
    monkeypatch.setattr(singleinstance,'acquire',lambda path: object())
    sleeps=[]
    monkeypatch.setattr(mod.time,'sleep',lambda seconds:sleeps.append(seconds))
    class Stopped(Exception): pass
    def stop_after_first_tick(): raise Stopped()
    monkeypatch.setattr(mod,'tick',stop_after_first_tick)
    with pytest.raises(Stopped): mod.main()
    assert sleeps == [120]

def test_startup_watchdog_launch_failure_is_reported(tmp_path, monkeypatch):
    source=Path(__file__).resolve().parents[1]/'setup_tasks.py'
    spec=importlib.util.spec_from_file_location('setup_tasks_failure_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    python=tmp_path/'pythonw.exe';python.touch()
    monkeypatch.setattr(mod,'ROOT',tmp_path)
    monkeypatch.setattr(mod,'VENV_PYW',python)
    monkeypatch.setattr(mod,'START_REQUEST_FILE',tmp_path/'data'/'logs'/'start_requested_at.txt')
    monkeypatch.setattr(mod,'read_mode',lambda:'startup-folder')
    messages=[]
    monkeypatch.setattr(mod,'log',lambda text:messages.append(text))
    monkeypatch.setattr(mod,'flush_log',lambda:None)
    calls=[]
    def popen(*args,**kwargs):
        calls.append(args)
        if len(calls)==2: raise OSError('watchdog simulated failure')
    monkeypatch.setattr(mod.subprocess,'Popen',popen)
    assert mod.start_now()==1
    assert len(calls)==2 and any('launch failed' in line for line in messages)

def test_setup_bat_does_not_report_success_when_immediate_start_fails():
    root=Path(__file__).resolve().parents[1]
    data=(root/'setup.bat').read_bytes()
    assert data.count(b'\n')==data.count(b'\r\n')
    start=data.index(b'setup_tasks.py --start\r\n')
    end=data.index(b'wait_start.py --timeout',start)
    block=data[start:end]
    assert b'exit /b 1' in block
    assert b'goto :finish' not in block
