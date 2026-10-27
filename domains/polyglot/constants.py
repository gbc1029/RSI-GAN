from pathlib import Path

# Constants - Evaluation Log Directories
BASE_IMAGE_BUILD_DIR = Path("./domains/polyglot/logs/build_images/base")
ENV_IMAGE_BUILD_DIR = Path("./domains/polyglot/logs/build_images/env")
INSTANCE_IMAGE_BUILD_DIR = Path("./domains/polyglot/logs/build_images/instances")
RUN_EVALUATION_LOG_DIR = Path("./domains/polyglot/logs/run_evaluation")

NPM_TEST_SH = """#!/bin/bash

# exit when any command fails
set -e

# Create symlinks if they don't exist
[ ! -e node_modules ] && ln -s /npm-install/node_modules .
[ ! -e package-lock.json ] && ln -s /npm-install/package-lock.json .


sed -i 's/\bxtest(/test(/g' *.spec.js
npm run test
"""

CPP_TEST_SH = """#!/bin/bash

# exit when any command fails
set -e

[ ! -d "build" ] && mkdir build
cd build
cmake -DEXERCISM_RUN_ALL_TESTS=1 -G "Unix Makefiles" ..
make
"""

NPM_TEST_COMMANDS = [
    "set -e",
    "[ ! -e node_modules ] && ln -s /npm-install/node_modules .",
    "[ ! -e package-lock.json ] && ln -s /npm-install/package-lock.json .",
    "sed -i 's/\\bxtest(/test(/g' *.spec.js",
    "npm run test"
]

CPP_TEST_COMMANDS = [
    "set -e",
    "[ ! -d \"build\" ] && mkdir build",
    "cd build", 
    "cmake -DEXERCISM_RUN_ALL_TESTS=1 -G \"Unix Makefiles\" ..",
    "make",
    "cd ../"
]


TEST_COMMANDS = {
    "python": ["pytest -rA --tb=long"],
    "rust": ["cargo test -- --include-ignored"],
    "go": ["go test ./..."],
    "javascript": NPM_TEST_COMMANDS,
    "cpp": CPP_TEST_COMMANDS, 
    "java": ["gradle test --no-daemon --offline"],
}

# ---------------------------------------------------------------------------
# F2: Gradle offline support (Java).
#
# Exercism java exercises ship a Gradle wrapper. On first run the wrapper
# downloads a distribution from services.gradle.org, which is structurally
# refused in this environment (unlike Maven Central, which is reachable).
# F2 therefore bakes the distribution zip into the env image at Gradle's
# wrapper cache path (so even `./gradlew` works offline), pre-warms a shared
# dependency cache by resolving the seed project at image build time, and
# runs the installed `gradle` binary directly (via PATH) which never touches
# the wrapper-download path at eval time.
# ---------------------------------------------------------------------------
GRADLE_WRAPPER_URL = "https://services.gradle.org/distributions/gradle-8.7-bin.zip"
GRADLE_DIST_NAME = "gradle-8.7"
GRADLE_DIST_ZIP_NAME = "gradle-8.7-bin.zip"
GRADLE_HOME_DIR = Path("/opt/gradle-home")
GRADLE_ARTIFACTS_DIR = Path(__file__).resolve().parent / "gradle_artifacts"

# F2: Docker image fragment for the Java environment. The env build context
# contains gradle-8.7-bin.zip and gradle_seed/ (staged by build_docker_images
# for Java). We extract the distribution into the wrapper's expected cache
# layout and warm the Maven Central dependency caches by running the seed
# project's tests once at image build time. At eval time the `gradle`
# executable (exposed on PATH) runs fully offline; nothing ever needs to
# reach services.gradle.org, which is structurally refused here.
GRADLE_ENV_EXTRA_DOCKERFILE = """
# F2: Gradle offline (baked distribution + warm dependency cache)
ENV GRADLE_USER_HOME=/opt/gradle-home \\
    PATH="/opt/gradle/gradle-8.7/bin:$PATH"

COPY ./gradle-8.7-bin.zip /root/gradle-8.7-bin.zip
COPY ./gradle_seed/ /opt/gradle_seed/
RUN mkdir -p /opt/gradle \\
    && python3 -m zipfile -e /root/gradle-8.7-bin.zip /opt/gradle \\
    && chmod -R +x /opt/gradle/gradle-8.7 \\
    && cd /opt/gradle_seed && gradle --no-daemon --offline test -q || true
"""

PYTHON_SPECS = {
    "python": "3.11",
    "pip_packages": [
        "flake8~=5.0.4",
        "pylint~=2.17.1", 
        "black==22.3.0",
        "yapf~=0.32.0",
        "tomli>=1.1.0",
        "pytest",
        "pytest-env",
        "pytest-asyncio",
        "async_timeout",
        "trio",
        "testpath"
    ],
    "test_cmd": " ".join(TEST_COMMANDS["python"]),
}

GENERIC_SPECS = {
    "rust": {
        "python": "3.11",
        "test_cmd": " ".join(TEST_COMMANDS["rust"])
    },
    "go": {
        "python": "3.11",
        "test_cmd": " ".join(TEST_COMMANDS["go"])
    },
    "javascript": {
        "python": "3.11",
        "test_cmd": "\n".join(TEST_COMMANDS["javascript"])
    },
    "cpp": {
        "python": "3.11",
        "test_cmd": "\n".join(TEST_COMMANDS["cpp"])
    },
    "java": {
        "python": "3.11",
        "test_cmd": " ".join(TEST_COMMANDS["java"]),
        "env_extras": [
            # F2: offline Gradle -- baked distribution + warm caches
            "export GRADLE_USER_HOME=/opt/gradle-home",
            "export GRADLE_OPTS='-Dorg.gradle.offline=true -Dorg.gradle.daemon=false'",
        ]
    }
}

MAP_REPO_VERSION_TO_SPECS = {
    "python": PYTHON_SPECS,
    "rust": GENERIC_SPECS["rust"],
    "go": GENERIC_SPECS["go"], 
    "javascript": GENERIC_SPECS["javascript"],
    "cpp": GENERIC_SPECS["cpp"],
    "java": GENERIC_SPECS["java"]
}

MAP_REPO_TO_INSTALL = {}

USE_X86 = {}