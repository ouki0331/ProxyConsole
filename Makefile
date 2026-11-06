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

clean:
	./scripts/down.sh
	rm -rf var/certificates
