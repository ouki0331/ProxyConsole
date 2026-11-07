.PHONY: certs up down config-test test clean

certs:
	./scripts/generate-certs.sh

up:
	./scripts/up.sh

down:
	./scripts/down.sh

config-test:
	./scripts/verify-config.sh

test:
	./tests/integration/test_proxy_chain.sh
	./tests/integration/test_capture_replay.sh
	./tests/integration/test_capture_index.sh
	./tests/integration/test_retention_cleanup.sh
	./tests/integration/test_route_management.sh
	./tests/integration/test_docker_discovery.sh
	./tests/integration/test_web_console.sh

clean:
	./scripts/down.sh
	rm -rf var/certificates var/captures var/blobs var/database
