moduledir = python_dir / 'klay'

configure_file(
  input: 'klay.in',
  output: binary_name,
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
)

install_subdir('importer', install_dir: moduledir)
install_subdir('utils', install_dir: moduledir)
install_subdir('store', install_dir: moduledir)
install_subdir('logging', install_dir: moduledir)
install_subdir('errors', install_dir: moduledir)
install_subdir('qt', install_dir: moduledir)
install_data(
  [
    'main.py',
    'window.py',
    'preferences.py',
    'details_dialog.py',
    'game.py',
    'game_cover.py',
    'extensions.py',
    configure_file(input: 'shared.py.in', output: 'shared.py', configuration: conf),
  ],
  install_dir: moduledir,
)
