scalable_dir = join_paths('hicolor', 'scalable', 'apps')
install_data(
  join_paths(scalable_dir, app_id + '.svg'),
  install_dir: join_paths(get_option('datadir'), 'icons', scalable_dir),
)

png_dir = join_paths('hicolor', '256x256', 'apps')
install_data(
  join_paths(png_dir, app_id + '.png'),
  install_dir: join_paths(get_option('datadir'), 'icons', png_dir),
)

symbolic_dir = join_paths('hicolor', 'symbolic', 'apps')
install_data(
  join_paths(symbolic_dir, app_id + '-symbolic.svg'),
  install_dir: join_paths(get_option('datadir'), 'icons', symbolic_dir),
)
