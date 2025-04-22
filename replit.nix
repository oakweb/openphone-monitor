{pkgs}: {
  deps = [
    pkgs.postgresql
    pkgs.openssl
    pkgs.sqlite-interactive
  ];
}
