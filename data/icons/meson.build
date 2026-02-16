fs = import('fs')

scalable_dir = join_paths('hicolor', 'scalable', 'apps')
png_dir = join_paths('hicolor', '256x256', 'apps')
symbolic_dir = join_paths('hicolor', 'symbolic', 'apps')
icon_ids = [app_id]
if app_id != base_app_id
  icon_ids += [base_app_id]
endif
# Add aliases so task managers can still resolve icons by WM class/binary name.
icon_ids += [binary_name, app_name]

foreach icon_id : icon_ids
  scalable_source = join_paths(scalable_dir, icon_id + '.svg')
  if not fs.exists(join_paths(meson.current_source_dir(), scalable_source))
    scalable_source = join_paths(scalable_dir, base_app_id + '.svg')
  endif

  png_source = join_paths(png_dir, icon_id + '.png')
  if not fs.exists(join_paths(meson.current_source_dir(), png_source))
    png_source = join_paths(png_dir, base_app_id + '.png')
  endif

  symbolic_source = join_paths(symbolic_dir, icon_id + '-symbolic.svg')
  if not fs.exists(join_paths(meson.current_source_dir(), symbolic_source))
    symbolic_source = join_paths(symbolic_dir, base_app_id + '-symbolic.svg')
  endif

  install_data(
    scalable_source,
    rename: icon_id + '.svg',
    install_dir: join_paths(get_option('datadir'), 'icons', scalable_dir),
  )

  install_data(
    png_source,
    rename: icon_id + '.png',
    install_dir: join_paths(get_option('datadir'), 'icons', png_dir),
  )

  install_data(
    symbolic_source,
    rename: icon_id + '-symbolic.svg',
    install_dir: join_paths(get_option('datadir'), 'icons', symbolic_dir),
  )
endforeach
