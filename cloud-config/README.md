Organization deployment configuration and build files
=====================================================
    When bin/deploy is run user_data is sent to AWS.  The user_data defines the type of deployment, 
    i.e. Demo, es cluster, frontend, etc.  The user_data is compiled in two stages.  The first 
    stage creates an Assembled Template.  By default the template is in memory but a bin/deploy
    argument exists that allows the assembled template to be saved and used later.  The second stage 
    adds Run Variables to the Assembled Template.  Then it can be sent as user_data to AWS to create
    an instance.

# Summary of cloud-config directory structure

#### Templates are assembled with ./template-parts
    # Standard Templates
    Demo/QA Demo                : app-es-pg-template.yml
    Cluster Frontend            : app-pg-template.yml
    Cluster Elasticsearch       : es-nodes-template.yml
    
    # Non Standard Templates
    Instance with remote pg     : app-es-template.yml
    Instance with remote pg/es  : app-template.yml

    Open one of the templates above to compare with ./template-parts.  Each variable '%(varname)s' 
    in the template has a matching file in ./template-parts.  Next is a way to view and save
    assembled templates.

    We can save Assembled Templates with the --save-configure bin/deploy argument.  It will 
    automaticallly determine which template to used based on input arguments.

    $ bin/deploy --save-config-name 20200430

    ### Output
    Created assembeled template
           ./cloud-config/assembled-templates/20200430-app-es-pg.yml
    Deploy with
           $ bin/deploy --use-prebuilt-config 20200430-app-es-pg
    Diff with on the fly assembly.  Does not deploy
           $ bin/deploy --use-prebuilt-config 20200430-app-es-pg --diff-configs
    ###


#### Directories:
    template-parts              : Pieces of the templates
    run-scripts                 : Install scripts runcmd_* template parts
    configs                     : Configuration files used in run-scripts, like apache, java, es
    assembled-templates         : Saved.  These still contains Run Varialbes

#### Helper Script
    create-ami.py               : Create amis from deployed --ami-build in AWS

#### Run_Variables
    * Run variables are in /etc/environment file on the instance.  
    * They are used in the run-scripts to configure the system and application builds
    * /etc/environment is loaded into login/ssh sessions so you can echo them on the instance.
    * The file will contain dupicate entries when deploying from an AMI.  Last ones are used.
    
    View them locally with --dry-run along with other info.  Does not deploy
    $ bin/deploy --dry-run

    Add options like --test, --release-candidate, or --candidate to see the differences in run vars.
    The ROLE should change along with other variables like ENCD_INDEX_PRIMARY, ENCD_INDEX_VIS,
    ENCD_INDEX_REGION.  The env vars are prefixed with ENCD_ as to not conflict with other env vars.

# Live Deployments
    Below we'll deploy demos and clusters using the --full-build argument to avoid needing amis.  
    Building from scratch is easier for cloud config development.

### QA/Development demo: app-es-pg-template.yml
    $ bin/deploy --full-build

        ### Output
        Deploying app-es-pg
        $ bin/deploy --full-build
        create instance and wait for running state
        ####

### Demo Cluster(es-wait): es-nodes-template.yml and app-pg-template

###### This command builds the Elasticsearch cluster
    $ export CLUSTER_NAME='encd-dev'
    $ bin/deploy --full-build --cluster-name "$CLUSTER_NAME" --es-wait
   
        ### Output
        Deploying es-nodes
        $ bin/deploy --full-build --cluster-name "$CLUSTER_NAME" --es-wait
        Create instance and wait for running state
        # Deploying Head ES Node(172.31.26.236): encd-dev-datamaster
        ###

    The IP address is the --es-ip used to deploy a frontend.
    $ export ES_IP='172.31.26.236'


###### This command builds the front-end machine that connects to the specified elasticsearch cluster
    $ bin/deploy --full-build --cluster-name "$CLUSTER_NAME" --es-ip "$ES_IP"
    
        ### Output
        Deploying app-pg
        $ bin/deploy --full-build --cluster-name encd-dev --es-ip 172.31.26.236
        Create instance and wait for running state

        Deploying Frontend(172.31.23.80): https://encd-dev.demo.encodedcc.org
        ###

### QA/Development demo with postgres pointing at Demo Cluster: app-pg-template.yml
    $ bin/deploy --full-build -n app-pg-pointing-at-es --es-ip "$ES_IP" --no-indexing


### Demo Cluster(es-wait) with open postgres port: es-nodes-template.yml and app-pg-template.yml

###### This command builds the Elasticsearch cluster
    $ export CLUSTER_NAME='v101x0-pretest'
    $ bin/deploy --full-build --cluster-name "$CLUSTER_NAME" --es-wait

        ### Output ###
            Deploying es-nodes with indexing=True
            $ bin/deploy --cluster-name v101x0-pretest --es-wait
            Create instance and wait for running state

            Deploying Head ES Node(172.31.26.243): v101x0-pretest-datamaster
             ssh ubuntu@i-07ee1fdef5a185169.instance.encodedcc.org

            Run the following command to view es head deployment log.
            ssh ubuntu@ec2-34-216-125-80.us-west-2.compute.amazonaws.com 'tail -f /var/log/cloud-init-output.log'

            Run the following command to view this es node deployment log.
            ssh ubuntu@ec2-35-164-150-246.us-west-2.compute.amazonaws.com 'tail -f /var/log/cloud-init-output.log'
            ES node1 ssh:
             ssh ubuntu@ec2-54-200-70-216.us-west-2.compute.amazonaws.com
            ES node2 ssh:
             ssh ubuntu@ec2-34-223-7-20.us-west-2.compute.amazonaws.com
            ES node3 ssh:
             ssh ubuntu@ec2-54-191-254-114.us-west-2.compute.amazonaws.com
            ES node4 ssh:
             ssh ubuntu@ec2-54-185-130-125.us-west-2.compute.amazonaws.com
            Done
        ###

    The IP address after 'Deploying Head ES Node' is the --es-ip used to deploy a frontend.
    $ export ES_IP='172.31.26.243'

###### This command builds the front-end machine that connects to the specified elasticsearch cluster with an open postgres port.
    $ bin/deploy --cluster-name "$CLUSTER_NAME" --es-ip "$ES_IP" --pg-open
    
        ### Output ###
            Deploying app-pg with indexing=True
            $ bin/deploy --cluster-name v101x0-pretest --es-ip 172.31.26.243 --pg-open
            Create instance and wait for running state

            Deploying Frontend(172.31.29.250): https://v101x0-pretest.demo.encodedcc.org
             ssh ubuntu@i-0d3c251746a95bfc7.instance.encodedcc.org


            Run the following command to view the deployment log.
            ssh ubuntu@ec2-34-217-43-68.us-west-2.compute.amazonaws.com 'tail -f /var/log/cloud-init-output.log'
            Done
        ###

    The IP address after 'Deploying Head ES Node' is the --es-ip used to deploy a frontend.
    $ export PG_IP='172.31.29.250'

### Demo with postgres and elasticsearch pointing at Demo Cluster: app-template.yml
    export FE_NAME_01='v101x0-pretest-fe1'
    $ bin/deploy -n "$FE_NAME_01" --cluster-name "$CLUSTER_NAME" --es-ip "$ES_IP" --pg-ip "$PG_IP"

        ### Output ###
            Deploying app-es with indexing=True
            $ bin/deploy -n v101x0-pretest-fe1 --cluster-name  --es-ip 172.31.26.243 --pg-ip 172.31.29.250
            Create instance and wait for running state

            Deploying Demo(172.31.25.53): https://v101x0-pretest-fe1.demo.encodedcc.org
             ssh ubuntu@i-02ab2c54b46ab7f2e.instance.encodedcc.org
            ssh and tail:
             ssh ubuntu@ec2-34-210-86-140.us-west-2.compute.amazonaws.com 'tail -f /var/log/cloud-init-output.log'
            Done
        ###

    export FE_NAME_02='v101x0-pretest-fe2'
    $ bin/deploy -n "$FE_NAME_02" --cluster-name "$CLUSTER_NAME" --es-ip "$ES_IP" --pg-ip "$PG_IP"


### (TBD) Demo with elasticsearch pointing at rds version of postgres: app-es-template.yml
