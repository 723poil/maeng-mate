"""업무 자료를 읽지 않고 PID 식별용 역할 명령을 유지한다."""

import signal

print("ready", flush=True)
signal.pause()
