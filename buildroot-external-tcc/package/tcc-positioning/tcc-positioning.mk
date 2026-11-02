################################################################################
#
# tcc-positioning
#
################################################################################

# O pacote usa o proprio repositorio (pasta acima de buildroot-external-tcc/)
TCC_POSITIONING_VERSION = $(shell head -n 1 $(BR2_EXTERNAL_TCC_PATH)/../VERSION | tr -d '\r')
TCC_POSITIONING_SITE = $(BR2_EXTERNAL_TCC_PATH)/..
TCC_POSITIONING_SITE_METHOD = local
TCC_POSITIONING_DEPENDENCIES = python3 python-serial

# Evita copiar para o diretorio de build o que nunca vai para a imagem
TCC_POSITIONING_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
	/venv /.venv /tests /deprecated /buildroot-external-tcc \
	__pycache__ .pytest_cache pytest_*.txt

# Somente estes itens vao para /opt/tcc
TCC_POSITIONING_APP_DIRS = hal acquisition coletor estimador experimental

TCC_POSITIONING_RSYNC_EXCLUDES = \
	--exclude='__pycache__/' --exclude='.pytest_cache/' \
	--exclude='tests/' --exclude='fixtures/' \
	--exclude='*.pyc' --exclude='*.pyo' --exclude='*.md' \
	--exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' \
	--exclude='*.csv' --exclude='*.log' --exclude='*.joblib' \
	--exclude='network_registry.json' --exclude='pytest_*.txt'

define TCC_POSITIONING_INSTALL_TARGET_CMDS
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/opt/tcc
	rsync -a --no-owner --no-group --chmod=D755,F644 \
		$(TCC_POSITIONING_RSYNC_EXCLUDES) \
		$(addprefix $(@D)/,$(TCC_POSITIONING_APP_DIRS)) \
		$(TARGET_DIR)/opt/tcc/
	$(INSTALL) -m 0644 $(@D)/software_version.py $(TARGET_DIR)/opt/tcc/software_version.py
	$(INSTALL) -m 0644 $(@D)/VERSION $(TARGET_DIR)/opt/tcc/VERSION
endef

$(eval $(generic-package))
