# utils/common.py

import subprocess

# Global dry-run flag
DRY_RUN = False

def set_dry_run(state: bool = True):
    """
    Enable or disable dry-run mode globally.
    """
    global DRY_RUN
    DRY_RUN = state

def run_command(command, cwd=None, silent=False):
    if DRY_RUN:
        cmd_str = ' '.join(command)
        print(f"🌐 [DRY-RUN] Would execute: {cmd_str} in {cwd}")

        if "git diff --cached" in cmd_str:
            # Pour dry-run, simuler diff vide pour les projets sans changements
            fake_diff = {
                "git diff --cached --name-only": "",
                "git diff --cached": ""
                }
            simulated_output = fake_diff.get(cmd_str, "")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=simulated_output, stderr="")
    else:
        if silent:
            return subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True)
