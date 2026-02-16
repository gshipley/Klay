scalable_dir = join_paths('hicolor', 'scalable', 'apps')
png_dir = join_paths('hicolor', '256x256', 'apps')
symbolic_dir = join_paths('hicolor', 'symbolic', 'apps')
icon_ids = [app_id]
if app_id != base_app_id
  icon_ids += [base_app_id]
endif

foreach icon_id : icon_ids
  install_data(
    join_paths(scalable_dir, icon_id + '.svg'),
    install_dir: join_paths(get_option('datadir'), 'icons', scalable_dir),
  )

  install_data(
    join_paths(png_dir, icon_id + '.png'),
    install_dir: join_paths(get_option('datadir'), 'icons', png_dir),
  )

  install_data(
    join_paths(symbolic_dir, icon_id + '-symbolic.svg'),
    install_dir: join_paths(get_option('datadir'), 'icons', symbolic_dir),
  )
endforeach
