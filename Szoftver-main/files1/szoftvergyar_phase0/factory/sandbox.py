"""Sandbox Runner (1.6, 11.5): eldobható, keményített konténer; indításkor önteszt; csendes visszalépés nincs."""
from __future__ import annotations
import os, shutil, subprocess, tempfile, uuid

MAX_OUTPUT_CHARS = 1_000_000      # az autofix JSON-ban adja vissza a módosított fájlokat; a kapuk hibasorait az error_lines úgyis szűri


class SandboxRefused(Exception):
    """Önteszt bukott vagy a kért runtime hiányzik: az indítás megtagadva, emberi döntés kell (4.6)."""

# Minden probe python-kód. Kilépési kód: 0 = a tiltott művelet SIKERÜLT (szökés); 1 = egyértelműen blokkolva (elvárt,
# a várt hibakóddal); minden más (2, kivétel, időtúllépés) = INKONKLUZÍV => bukás (fail-closed).
# Helyettesítők: @CANARY_PATH@, @CANARY_TOKEN@ (host-canary), @PIDS_LIMIT@ (a beállított pids_limit).
PROBES = {
    "write_rootfs": (
        "import sys,errno\n"
        "try:\n open('/rootfs_probe','w').write('x'); sys.exit(0)\n"
        "except OSError as e: sys.exit(1 if e.errno in (errno.EROFS, errno.EACCES, errno.EPERM) else 2)\n"
        "except Exception: sys.exit(2)"),
    "outbound_network": (
        "import socket,sys,errno\n"
        "try:\n socket.create_connection(('1.1.1.1',53),timeout=2); sys.exit(0)\n"
        "except socket.timeout: sys.exit(2)\n"
        "except OSError as e: sys.exit(1 if e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EADDRNOTAVAIL, errno.EPERM, errno.EACCES) else 2)\n"
        "except Exception: sys.exit(2)"),
    # A hoston a runner létrehoz egy canary fájlt egyedi tartalommal; ha a konténerből ugyanazon az úton ugyanaz a tartalom
    # olvasható, akkor host-útvonal szivárog be (bind mount). A docker.sock jelenléte is szökés.
    "read_host_path": (
        "import sys,os\n"
        "try:\n"
        " if os.path.exists('/var/run/docker.sock') or os.path.exists('/run/docker.sock'): sys.exit(0)\n"
        " try: t=open('@CANARY_PATH@').read()\n"
        " except (FileNotFoundError, PermissionError): sys.exit(1)\n"
        " sys.exit(0 if '@CANARY_TOKEN@' in t else 1)\n"
        "except Exception: sys.exit(2)"),
    "mount_syscall": (
        "import ctypes,sys,errno\n"
        "try:\n"
        " libc=ctypes.CDLL(None,use_errno=True)\n"
        " r=libc.mount(b'none',b'/mnt',b'tmpfs',0,None)\n"
        " if r==0: sys.exit(0)\n"
        " sys.exit(1 if ctypes.get_errno() in (errno.EPERM, errno.EACCES) else 2)\n"
        "except Exception: sys.exit(2)"),
    # A pids_limit kétszeresét próbálja elforkolni: ha sikerül, nincs korlát (szökés); ha EAGAIN, blokkolva; egyéb hiba inkonkluzív.
    "exceed_pids_limit": (
        "import os,sys,time,errno\n"
        "kids=[]; res=0\n"
        "try:\n"
        " for _ in range(@PIDS_LIMIT@*2):\n"
        "  pid=os.fork()\n"
        "  if pid==0:\n"
        "   time.sleep(3); os._exit(0)\n"
        "  kids.append(pid)\n"
        "except OSError as e:\n"
        " res=1 if e.errno==errno.EAGAIN else 2\n"
        "except Exception:\n"
        " res=2\n"
        "for k in kids:\n"
        " try: os.kill(k,9)\n"
        " except Exception: pass\n"
        " try: os.waitpid(k,0)\n"
        " except Exception: pass\n"
        "sys.exit(res)"),
}

def _tar_children(path):
    import io, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for e in sorted(os.listdir(path)):
            tf.add(os.path.join(path, e), arcname=e)
    return buf.getvalue()

class DockerBackend:
    """Valódi Docker-háttér. Hálózat nélkül a `docker cp` -> `start` -> `cp` -> `rm` folyamat: host-mount nincs."""
    def __init__(self, sb_cfg: dict):
        self.c = sb_cfg

    def available(self, runtime: str) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            out = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True, timeout=20).stdout
            return runtime == "runc" or runtime in out
        except Exception:
            return False

    def _args(self, name: str, interactive: bool = False) -> list[str]:
        c, h = self.c, self.c.get("hardening", {})
        a = ["docker", "create"] + (["-i"] if interactive else []) + ["--name", name, "--runtime", c["oci_runtime"], "--network", "none",
             "--read-only", "--user", c.get("run_as_user", "nobody"), "--cpus", str(c.get("cpu_limit", "2")),
             "--memory", str(c.get("memory_limit", "2g")), "--pids-limit", str(h.get("pids_limit", 256)),
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        for t in h.get("tmpfs", ["/tmp"]):
            a += ["--tmpfs", t]
        a += ["--tmpfs", "/work:rw,nosuid,nodev,size=" + str(h.get("work_tmpfs_size", "512m")) + ",mode=1777"]
        a += ["-w", "/work", c["image"]]
        return a

    def run(self, argv: list[str], worktree: str | None = None, timeout: int = 600) -> tuple[int, str]:
        name = "fsz-" + uuid.uuid4().hex[:10]
        try:
            data, start_cmd, run_argv = None, ["docker", "start", "-a", name], argv
            if worktree:
                # A gyökér csak-olvasható, ezért `docker cp` nem használható: a fájlok tar-ként, stdin-en
                # érkeznek, és a konténeren belül a /work tmpfs-be csomagolódnak ki (host-mount nincs).
                data = _tar_children(worktree)
                start_cmd = ["docker", "start", "-a", "-i", name]
                run_argv = ["sh", "-c", 'tar -x -m --no-overwrite-dir --no-same-owner -C /work && exec "$@"', "sh"] + argv
            subprocess.run(self._args(name, interactive=bool(worktree)) + run_argv, check=True, capture_output=True, timeout=60)
            p = subprocess.run(start_cmd, input=data, capture_output=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")[-MAX_OUTPUT_CHARS:]
        except subprocess.TimeoutExpired:
            return 124, "sandbox timeout"
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)   # destroy_after_run

class SandboxRunner:
    def __init__(self, cfg: dict, backend=None, audit=None, kill=None):
        self.c = cfg["sandbox"]
        self.backend = backend or DockerBackend(self.c)
        self.audit, self.kill = audit, kill
        self.selftest_ok = False
        self.image_seen = None

    def start(self):
        rt = self.c["oci_runtime"]
        if not self.backend.available(rt):
            if self.c.get("on_runtime_unavailable", "halt_and_ask_human") == "halt_and_ask_human":
                if self.audit: self.audit.append("sandbox", "RUNTIME_UNAVAILABLE", {"runtime": rt})
                raise SandboxRefused(f"a kért runtime nem érhető el: {rt} (csendes visszalépés nincs)")
        if rt == "runc" and not self.c.get("acknowledged_weaker_isolation"):
            raise SandboxRefused("runc használatához acknowledged_weaker_isolation: true kell (naplózva)")
        self.selftest()

    def _probe_code(self, name: str, canary_path: str, canary_token: str) -> str:
        pids = int(self.c.get("hardening", {}).get("pids_limit", 256))
        return (PROBES[name].replace("@CANARY_PATH@", canary_path).replace("@CANARY_TOKEN@", canary_token)
                .replace("@PIDS_LIMIT@", str(pids)))

    def selftest(self) -> dict:
        """Minden probe: rc==1 -> BLOCKED; rc==0 -> ESCAPED; bármi más -> INCONCLUSIVE. Csak a BLOCKED jó (fail-closed)."""
        st = self.c.get("selftest", {})
        res, diag = {}, {}
        cdir = tempfile.mkdtemp(prefix="fsz-canary-")
        token = "CANARY-" + uuid.uuid4().hex
        cpath = os.path.join(cdir, "canary.txt")
        try:
            with open(cpath, "w") as f:
                f.write(token)
            os.chmod(cdir, 0o755); os.chmod(cpath, 0o644)
            for name in st.get("must_fail", list(PROBES)):
                rc, out = self.backend.run(["python3", "-c", self._probe_code(name, cpath, token)], timeout=60)
                res[name] = "BLOCKED" if rc == 1 else ("ESCAPED" if rc == 0 else "INCONCLUSIVE")
                if res[name] != "BLOCKED":
                    diag[name] = {"rc": rc, "out": (out or "")[-300:]}
        finally:
            shutil.rmtree(cdir, ignore_errors=True)
        rc, out = self.backend.run(["python3", "-c", "import os;print(os.getuid())"], timeout=30)
        res["non_root_user"] = "OK" if rc == 0 and out.strip() not in ("0", "") else "FAIL"
        if res["non_root_user"] != "OK":
            diag["non_root_user"] = {"rc": rc, "out": (out or "")[-300:]}
        bad = {k: v for k, v in res.items() if v not in ("BLOCKED", "OK")}
        if self.audit: self.audit.append("sandbox", "SELFTEST", {"result": res, "diagnostics": diag})
        if bad and st.get("on_fail", "refuse_to_start") == "refuse_to_start":
            self.selftest_ok = False
            raise SandboxRefused(f"önteszt bukott: {bad} (részletek: {diag})")
        self.selftest_ok = not bad
        self.image_seen = self.c["image"]
        return res

    def run(self, argv: list[str], worktree: str | None = None) -> tuple[int, str]:
        if self.kill: self.kill.check()
        if not self.selftest_ok:
            raise SandboxRefused("önteszt nélkül nincs futtatás")
        if self.c["image"] != self.image_seen:   # image-váltás -> újra önteszt (11.5)
            self.selftest()
        return self.backend.run(argv, worktree, self.c.get("timeout_seconds", 600))

    def destroy_all(self):
        """Kill switchnél/HALT-nál: futó konténerek megsemmisítése (6.9)."""
        if shutil.which("docker"):
            subprocess.run("docker ps -aq --filter name=fsz- | xargs -r docker rm -f", shell=True, capture_output=True)
