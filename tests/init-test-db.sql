CREATE ROLE inventra_test LOGIN PASSWORD 'inventra_test' NOCREATEDB;
GRANT CONNECT, CREATE ON DATABASE inventra_test TO inventra_test;
