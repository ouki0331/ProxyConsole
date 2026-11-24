.PHONY: certs up down config-test runtime-test test ci-local readiness-check clean

certs:
	./scripts/generate-certs.sh

up:
	./scripts/up.sh

down:
	./scripts/down.sh

config-test:
	./scripts/verify-config.sh

runtime-test:
	./tests/unit/test_container_runtime.sh
	python3 -m unittest tests.unit.test_discovery_provider

test:
	./tests/integration/test_proxy_chain.sh
	./tests/integration/test_capture_replay.sh
	./tests/integration/test_capture_index.sh
	./tests/integration/test_retention_cleanup.sh
	./tests/integration/test_route_management.sh
	./tests/integration/test_docker_discovery.sh
	./tests/integration/test_web_console.sh
	./tests/integration/test_request_draft_replay.sh
	./tests/integration/test_web_request_console.sh
	./tests/integration/test_modification_rules.sh
	./tests/integration/test_web_rules_console.sh
	./tests/integration/test_notifications_console.sh
	./tests/integration/test_overview_i18n_navigation.sh

ci-local:
	./scripts/ci-local.sh

readiness-check:
	./scripts/check-company-readiness.sh

clean:
	./scripts/down.sh
	rm -rf var/certificates var/captures var/blobs var/database
