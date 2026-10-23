# SAM "makefile" build method (Metadata.BuildMethod: makefile on the
# WatchEvaluatorStreamFunction resource in ../../template.yaml) — this is
# a standard, documented SAM build method, not custom tooling:
# https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-using-build-with-makefile.html
#
# Why this function needs a Makefile build instead of SAM's default
# "install requirements.txt from CodeUri" behavior: its real code lives in
# backend/app/lambda_handlers/watch_evaluator_stream.py, reusing
# backend/app/{domain,repositories,services}/* directly — the SAME
# modules the long-running backend process imports — rather than a
# duplicate copy that could silently drift out of sync with the tested
# implementation (see app/lambda_handlers/__init__.py's module docstring
# for why that single-source-of-truth property matters here).
#
# `sam build` first copies this directory's contents (CodeUri) into an
# isolated scratch directory, THEN runs `make` there with $(CURDIR) set to
# that scratch copy and $(ARTIFACTS_DIR) set to the real build output
# directory — a plain relative `../backend/app` would therefore resolve
# outside the scratch copy entirely (confirmed empirically: it lands under
# the *filesystem's* root, not the repository's). The `app` and
# `scenarios` entries in THIS directory are symlinks to
# ../../../backend/app and ../../../scenarios respectively; the copy step
# dereferences symlinks (copies their real target content, not the link),
# so by the time `make` runs, `./app` and `./scenarios` already ARE full,
# real copies of backend/app and scenarios/ — no traversal outside the
# scratch copy is needed at all.
build-WatchEvaluatorStreamFunction:
	python3 -m pip install --no-cache-dir -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp -r app "$(ARTIFACTS_DIR)/app"
	cp -r scenarios "$(ARTIFACTS_DIR)/scenarios"
	find "$(ARTIFACTS_DIR)/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
