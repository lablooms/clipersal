"""Real end-to-end smoke test: launch the actual app (temp clips/buffer dirs,
own IPC port, no tray), record ~12 s, then SAVE / SAVE-trim / SCREENSHOT /
STATS over real IPC and verify the artifacts with ffprobe. Cleans up after.
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from clipersal import ipc_client  # noqa: E402

PORT = 51999
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def payload(reply: str) -> str:
    return reply[3:] if reply.startswith("OK ") else reply


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="clipersal-smoke-"))
    clips = work / "clips"
    err_log = work / "app-stderr.log"
    app = None
    try:
        with open(err_log, "wb") as err:
            app = subprocess.Popen(
                [
                    "clipersal",
                    "--clips-dir", str(clips),
                    "--ipc-port", str(PORT),
                    "--no-tray",
                    "--no-check-for-updates",
                    "--buffer-seconds", "30",
                ],
                stdout=err,
                stderr=err,
            )
        # Wait for the IPC server (encoder smoke-encode can take a while).
        pong = False
        for attempt in range(60):
            try:
                reply = ipc_client.send_command("PING", port=PORT, timeout=1.0)
                pong = payload(reply) == "PONG"
                if pong:
                    break
                print(f"  ping#{attempt}: reply={reply!r}", flush=True)
            except Exception as exc:
                print(f"  ping#{attempt}: {type(exc).__name__}: {exc} (poll={app.poll()})", flush=True)
                time.sleep(1.0)
            if app.poll() is not None:
                print(f"  app exited (code={app.returncode}) after {attempt + 1} attempts", flush=True)
                break
        check("app starts + IPC up", pong, f"pid={app.pid}")
        if not pong:
            if err_log.exists():
                print("--- app stderr ---", flush=True)
                print(err_log.read_text(errors="replace")[-3000:], flush=True)
            return 1

        status = payload(ipc_client.send_command("STATUS", port=PORT, timeout=5.0))
        check("STATUS is RECORDING", status == "RECORDING", status)

        print("recording 12 s...", flush=True)
        time.sleep(12)

        stats = ipc_client.send_command("STATS", port=PORT, timeout=5.0)
        parsed = ipc_client.parse_stats_payload(stats)
        check("STATS parses", parsed.get("state") == "RECORDING" and parsed.get("segments") != "", stats)

        saved = payload(ipc_client.send_command("SAVE", port=PORT, timeout=ipc_client.SAVE_TIMEOUT))
        saved_path = Path(saved)
        check("SAVE returns a path", saved_path.suffix == ".mp4" and saved_path.exists(), saved)
        if saved_path.exists():
            check("clip is non-trivial", saved_path.stat().st_size > 100_000, f"{saved_path.stat().st_size} B")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(saved_path)],
                capture_output=True, text=True, timeout=30,
            )
            try:
                dur = float(probe.stdout.strip())
            except ValueError:
                dur = 0.0
            check("clip duration >= 8 s", dur >= 8.0, f"{dur:.1f} s")

        trimmed = payload(ipc_client.send_command("SAVE", arg="5", port=PORT, timeout=ipc_client.SAVE_TIMEOUT))
        check("SAVE 5 (trim) works", Path(trimmed).exists(), trimmed)

        shot = payload(ipc_client.send_command("SCREENSHOT", port=PORT, timeout=30.0))
        shot_path = Path(shot)
        check("SCREENSHOT returns a png", shot_path.suffix == ".png" and shot_path.exists(), shot)
        if shot_path.exists():
            check("screenshot non-empty", shot_path.stat().st_size > 10_000, f"{shot_path.stat().st_size} B")

        bye = payload(ipc_client.send_command("QUIT", port=PORT, timeout=5.0))
        check("QUIT ack", bye == "bye", bye)
        try:
            app.wait(timeout=20)
            check("app exits cleanly", True, f"code={app.returncode}")
        except subprocess.TimeoutExpired:
            check("app exits cleanly", False, "did not exit in 20 s")
            app.kill()
        return 0 if all(ok for _, ok, _ in results) else 1
    finally:
        if app is not None and app.poll() is None:
            app.kill()
        shutil.rmtree(work, ignore_errors=True)
        print(f"\nsummary: {sum(1 for _, ok, _ in results if ok)}/{len(results)} passed", flush=True)


if __name__ == "__main__":
    sys.exit(main())
