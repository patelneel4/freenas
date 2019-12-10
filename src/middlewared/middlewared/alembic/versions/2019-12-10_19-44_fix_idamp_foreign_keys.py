"""empty message

Revision ID: 17fe2353a0de
Revises: dcf5c178714b
Create Date: 2019-12-10 19:44:44.434836+00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '17fe2353a0de'
down_revision = 'dcf5c178714b'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('directoryservice_idmap_ad', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_ad_idmap_ad_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_ad_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')

    with op.batch_alter_table('directoryservice_idmap_autorid', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_autorid_idmap_autorid_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_autorid_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')

    with op.batch_alter_table('directoryservice_idmap_ldap', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_ldap_idmap_ldap_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_ldap_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')

    with op.batch_alter_table('directoryservice_idmap_nss', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_nss_idmap_nss_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_nss_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')

    with op.batch_alter_table('directoryservice_idmap_rfc2307', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_rfc2307_idmap_rfc2307_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_rfc2307_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')

    with op.batch_alter_table('directoryservice_idmap_rid', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_rid_idmap_rid_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_rid_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')

    with op.batch_alter_table('directoryservice_idmap_script', schema=None) as batch_op:
        batch_op.create_foreign_key(batch_op.f('fk_directoryservice_idmap_script_idmap_script_domain_id_directoryservice_idmap_domain'), 'directoryservice_idmap_domain', ['idmap_script_domain_id'], ['idmap_domain_name'], ondelete='CASCADE')
