"""Kill switch (6.9): /state/KILL fájl vagy `factory stop --now`; minden ágens minden lépés előtt ellenőrzi."""
import os

class Killed(Exception):
    pass

class KillSwitch:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "KILL")
        os.makedirs(state_dir, exist_ok=True)

    def trip(self, reason: str = "manual"):
        with open(self.path, "w") as f:
            f.write(reason)

    def clear(self):
        if os.path.exists(self.path): os.remove(self.path)

    @property
    def tripped(self) -> bool:
        return os.path.exists(self.path)

    def check(self):
        if self.tripped:
            with open(self.path) as f:
                raise Killed(f.read())
