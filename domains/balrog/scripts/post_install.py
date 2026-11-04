"""Balrog DATA post-install (Boxoban levels + TextWorld games).

Only the ``minihack`` and ``textworld`` environments need this: it downloads
Boxoban levels from GitHub and TextWorld game archives from Google Drive.

NOT required for ``balrog_babyai`` / ``balrog_babaisai`` -- those two are the
only balrog domains wired into the GAN path, and their Python dependencies are
installed by ``setup_balrog.sh`` next to this file (see README "3d) balrog
domain"). Running this script needs external network and belongs to the
(unwired) minihack/textworld environments.
"""

import os
import subprocess
import zipfile

import pkg_resources

DESTINATION_PATH = pkg_resources.resource_filename("minihack", "dat")
BOXOBAN_REPO_URL = "https://github.com/deepmind/boxoban-levels/archive/refs/heads/master.zip"


def download_boxoban_levels():
    print("Downloading Boxoban levels...")
    os.system(f"wget -c --read-timeout=5 --tries=0 " f'"{BOXOBAN_REPO_URL}" -P {DESTINATION_PATH}')
    print("Boxoban levels downloaded, unpacking...")

    zip_file = os.path.join(DESTINATION_PATH, "master.zip")
    with zipfile.ZipFile(zip_file, "r") as zip_ref:
        zip_ref.extractall(DESTINATION_PATH)

    os.remove(zip_file)


def download_textworld_levels():
    import requests

    url = "https://drive.google.com/uc?export=download&id=1aeT-45-OBxiHzD9Xn99E5OvC86XmqhzA"
    response = requests.get(url)
    zip_path = "tw-games.zip"
    with open(zip_path, "wb") as f:
        f.write(response.content)
    subprocess.run(["unzip", zip_path, "-d", "./domains/balrog"])
    os.remove(zip_path)


def main():
    download_boxoban_levels()
    download_textworld_levels()


if __name__ == "__main__":
    main()
