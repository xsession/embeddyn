"""Screenshots of the embeddyn Lab for the documentation (docs/images/lab-*.png).

Start the Lab first, then run this script against it:

    embeddyn mechatronics gui --port 8710 &
    python tools/docs/capture_lab.py http://127.0.0.1:8710

Needs Selenium and a headless Firefox with geckodriver (GECKODRIVER may point
at it; the snap Firefox ships one at /snap/bin/geckodriver).
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
EXAMPLES = ROOT / "examples" / "mechatronics"


def browser() -> webdriver.Firefox:
    options = webdriver.FirefoxOptions()
    options.add_argument("-headless")
    options.add_argument("--width=1600")
    options.add_argument("--height=980")
    driver_path = os.environ.get("GECKODRIVER") or shutil.which("geckodriver")
    service = Service(executable_path=driver_path) if driver_path else Service()
    driver = webdriver.Firefox(options=options, service=service)
    driver.set_window_size(1600, 980)
    return driver


def wait(driver, condition, timeout=60.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if driver.execute_script(condition):
                return
        except Exception:  # page still loading
            pass
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}")


def open_project(driver, url: str, project: Path) -> None:
    driver.get(f"{url}/#open={project}")
    driver.refresh()            # the hash alone does not reload an already open page
    wait(driver, f"return document.getElementById('project-name').textContent.includes({project.stem!r})",
         what=f"{project.name} to open")
    time.sleep(1.0)


def click(driver, selector: str) -> None:
    driver.find_element(By.CSS_SELECTOR, selector).click()
    time.sleep(0.4)


def run_to_end(driver, timeout: float) -> None:
    click(driver, "#btn-run")
    wait(driver, "return ['done','stopped','error'].some(s => document.getElementById('sim-status').textContent.includes(s))",
         timeout=timeout, what="the run to finish")
    time.sleep(1.5)


def shot(driver, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Show paths relative to the embeddyn root, not the machine the pictures were taken on.
    driver.execute_script("""
        const root = arguments[0];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode())
            if (node.nodeValue.includes(root)) node.nodeValue = node.nodeValue.split(root).join('');
    """, str(ROOT) + "/")
    driver.save_screenshot(str(OUT / name))
    print("wrote", OUT / name)


def main(url: str) -> int:
    driver = browser()
    try:
        # 1. Schematic, instruments and 3-D servo (python parts only: fast).
        open_project(driver, url, EXAMPLES / "rc-servo-lab.yaml")
        click(driver, "#btn-fit")
        shot(driver, "lab-schematic.png")
        run_to_end(driver, 120)
        shot(driver, "lab-instruments.png")
        click(driver, "#view-tabs button[data-view='split']")
        time.sleep(1.0)
        shot(driver, "lab-split.png")
        click(driver, "#dock-tabs button[data-dock='metrics']")
        shot(driver, "lab-metrics.png")

        # 2. MuJoCo arm in the 3-D view.
        open_project(driver, url, EXAMPLES / "mujoco-arm.yaml")
        run_to_end(driver, 180)
        click(driver, "#view-tabs button[data-view='scene']")
        time.sleep(1.0)
        shot(driver, "lab-3d-arm.png")

        # 3. Firmware-in-the-loop arm: the schematic generated from the project file.
        open_project(driver, url, EXAMPLES / "mujoco-arm-firmware.yaml")
        click(driver, "#view-tabs button[data-view='schematic']")
        click(driver, "#btn-fit")
        shot(driver, "lab-arm-firmware-schematic.png")
    finally:
        driver.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8710"))
