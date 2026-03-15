project(
  'klay',
  version: '0.2.3',
  meson_version: '>= 0.59.0',
  default_options: [
    'warning_level=2',
    'werror=false',
  ],
)

# Qt frontend (PySide6) is loaded at runtime by the Python launcher.

# Translations are currently disabled on Windows due gettext/MSYS2 issues.
if host_machine.system() != 'windows'
  i18n = import('i18n')
endif

gnome = import('gnome')
python = import('python')

py_installation = python.find_installation('python3')

app_name = 'Klay'
package_name = 'Klay'
binary_name = 'klay'
data_dir_name = 'klay'
gettext_domain = 'klay'
app_categories = 'KDE;Game;PackageManager;'
base_app_id = 'com.grantshipley.Klay'
base_prefix = '/com/grantshipley/Klay'

python_dir = join_paths(get_option('prefix'), py_installation.get_install_dir())
pkgdatadir = join_paths(get_option('prefix'), get_option('datadir'), data_dir_name)
libexecdir = join_paths(get_option('prefix'), get_option('libexecdir'))

profile = get_option('profile')
if profile == 'development'
  app_id = base_app_id + '.Devel'
  prefix = base_prefix + '/Devel'
elif profile == 'release'
  app_id = base_app_id
  prefix = base_prefix
endif

conf = configuration_data()
conf.set('PYTHON', py_installation.full_path())
conf.set('PYTHON_VERSION', py_installation.language_version())
conf.set('APP_ID', app_id)
conf.set('APP_NAME', app_name)
conf.set('APP_CATEGORIES', app_categories)
conf.set('BINARY_NAME', binary_name)
conf.set('RESOURCE_BASENAME', data_dir_name)
conf.set('DATA_DIR_NAME', data_dir_name)
conf.set('GETTEXT_DOMAIN', gettext_domain)
conf.set('FLAVOR', 'klay')
conf.set('PREFIX', prefix)
conf.set('VERSION', meson.project_version())
conf.set('PROFILE', profile)
conf.set('TIFF_COMPRESSION', get_option('tiff_compression'))
conf.set('localedir', join_paths(get_option('prefix'), get_option('localedir')))
conf.set('pkgdatadir', pkgdatadir)
conf.set('libexecdir', libexecdir)

subdir('data')
subdir('klay')

# Runtime media/images used by the Qt UI (splash, source icons, etc.).
install_subdir('assets', install_dir: pkgdatadir)

if host_machine.system() == 'windows'
  subdir('build-aux/windows')
else
  subdir('po')
endif

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
