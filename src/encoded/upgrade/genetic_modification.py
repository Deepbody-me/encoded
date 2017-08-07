from snovault import (
    CONNECTION,
    upgrade_step
)


@upgrade_step('genetic_modification', '1', '2')
def genetic_modification_1_2(value, system):
    # http://redmine.encodedcc.org/issues/3063
    if 'modifiction_description' in value:
        value['modification_description'] = value['modifiction_description']
        value.pop('modifiction_description')


@upgrade_step('genetic_modification', '2', '3')
def genetic_modification_2_3(value, system):
    # http://redmine.encodedcc.org/issues/4448
    if 'modification_description' in value:
        value['description'] = value['modification_description']
        value.pop('modification_description')

    if 'modification_zygocity' in value:
        value['zygosity'] = value['modification_zygocity']
        value.pop('modification_zygocity')

    if 'modification_purpose' in value:
        value['purpose'] = value['modification_purpose']
        value.pop('modification_purpose')

    if 'modification_genome_coordinates' in value:
        value['modified_site'] = value['modification_genome_coordinates']
        value.pop('modification_genome_coordinates')

    if 'modification_treatments' in value:
        value['treatments'] = value['modification_treatments']
        value.pop('modification_treatments')


@upgrade_step('genetic_modification', '5', '6')
def genetic_modification_5_6(value, system):
    # https://encodedcc.atlassian.net/browse/ENCD-3088

    conn = system['registry'][CONNECTION]

    if 'target' in value:
        value['modified_site_by_target_id'] = value['target']
        value.pop('target')

    if 'modified_site' in value:
        value['modified_site_by_coordinates'] = value['modified_site']
        value.pop('modified_site')

    rep_obj = dict()
    if 'source' in value:
        # If for some inexplicable reason, there is a source associated with the genetic_modification,
        # let's move it to reagent repository for now. If there is one in the technique, we'll overwrite it
        # and use that one instead.
        rep_obj['repository'] = value['source']
        value.pop('source')

    if 'product_id' in value:
        # If for some inexplicable reason, there is a product_id associated with the genetic_modification,
        # let's move it to reagent identifiers for now. If there is one in the technique, we'll overwrite it
        # and use those instead.
        rep_obj['identifier'] = value['product_id']
        value.pop('product_id')
    if rep_obj:
        value['reagent_availability'].add(rep_obj)

    # New required properties modification_technique and purpose need to be handled somehow
    if value['modification_techniques']:
        alias_flag = False
        for t in value['modification_techniques']:
            technique = conn.get_by_uuid(t)
            if 'aliases' in technique.properties:
                alias_flag = True
            rep_obj = dict()
            if 'source' in technique.properties:
                rep_obj['repository'] = technique.properties['source']
            if 'product_id' in technique.properties:
                rep_obj['identifier'] = technique.properties['product_id']
            if rep_obj:
                value['reagent_availability'].add(rep_obj)
            if 'guide_rna_sequences' in technique.properties:
                value['guide_rna_sequences'] = technique.properties['guide_rna_sequences']
                value['modification_technique'] = 'CRISPR'

                if 'insert_sequence' in technique.properties:
                    value['introduced_sequence'] = technique.properties['insert_sequence']
                if alias_flag:
                    for a in technique.properties['aliases']:
                        b = a + '-CRISPR'
                        if 'aliases' in value:
                            value['aliases'].append(b)
                        else:
                            value['aliases'] = [b]
            elif 'talen_platform' in technique.properties:
                value['modification_technique'] = 'TALE'
                # We think these should have purpose = repression if empty. For the purposes
                # of the upgrade, let's add that in for now.
                if 'purpose' not in value:
                    value['purpose'] = 'repression'
                if 'notes' in value:
                    value['notes'] = value['notes'] + '. TALEN platform: ' + technique.properties['talen_platform']
                else:
                    value['notes'] = 'TALEN platform ' + technique.properties['talen_platform']
                if 'target_sequence' in technique.properties:
                    # These won't be in the same order as the RVD_sequences but at least
                    # it will save the manual migration. We can't enforce the same order anyway.
                    if 'targeted_sequences' in value:
                        value['targeted_sequences'].append(technique.properties['target_sequence'])
                    else:
                        value['targeted_sequences'] = [technique.properties['target_sequence']]
                if alias_flag:
                    for a in technique.properties['aliases']:
                        b = a + '-TALE'
                        if 'aliases' in value:
                            value['aliases'].append(b)
                        else:
                            value['aliases'] = [b]
            else:
                # This shouldn't happen as we currently don't have any other possible techniques
                # so let's just set it to something we know we don't have yet annotated correctly
                # in the data so we can identify special cases to deal with
                value['modification_technique'] = 'mutagenesis'
    else:
        value['modification_technique'] = 'mutagenesis'

    if 'modification_techniques' in value:
        # These will no longer be linked out to the respective technique objects. The
        # migration will have to happen with a manual patch to move those properties
        # into new ones in genetic_modification.json
        value.pop('modification_techniques')

    if 'purpose' not in value:
        # This shouldn't happen as we currently don't have any GM objects missing purpose,
        # so let's just set it to something we know we don't have yet annotated in the data so
        # we can identify any special cases we might need to deal with
        value['purpose'] = 'analysis'
