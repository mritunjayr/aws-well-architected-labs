"""
# Integration test for Cost Optimization Data Collection

## About
    This test will:
    - deploy Cost Optimization Data Collection stacks (all in one account)
    - update all nested stacks to the git version
    - trigger collection
    - test that collection works  (tables are not empty)
    - delete all stacks and tables

## Prerequsites in account:
    1. Activate Organizations
    2. Opt-In Compute Optimizer
    3. Activate Business or Enterprise Support (for ta collection only)
    4. Create:
        RDS instace, Budget, Unattached EBS, ECS cluster with at least 1 Service,
    FIXME: add CFM for Prerequsites

## Install:
    pip3 install cfn-flip boto3 pytest

## Run (expect 15 mins):
Pytest:

    pytest static/Cost/300_Optimization_Data_Collection/Test/test-from-scratch.py
       --log_cli_format="%(asctime)s [%(levelname)8s] %(message)s"
       --log_cli=true \
       --log-level=INFO -s
      

Python:
    python3 static/Cost/300_Optimization_Data_Collection/Test/test-from-scratch.py 


"""
import os
import time
import datetime
import json
import logging
from textwrap import indent

import boto3


BUCKET = os.environ.get('BUCKET', "aws-wa-labs-staging")
logger = logging.getLogger(__name__)
account_id = boto3.client("sts").get_caller_identity()["Account"]
start_time = None

cloudformation = boto3.client('cloudformation')
athena = boto3.client('athena')
s3 = boto3.resource("s3")

HEADER = '\033[95m'
BLUE = '\033[94m'
CYAN = '\033[96m'
GREEN = '\033[92m'
WARNING = '\033[93m'
RED = '\033[91m'
END = '\033[0m'
BOLD = '\033[1m'
UNDERLINE = '\033[4m'

def watch_stacks(stack_names = []):
    ''' watch stacks while they are IN_PROGRESS and/or until they are deleted'''
    last_update = {stack_name: None for stack_name in stack_names}
    while True:
        in_progress = False
        for stack_name in stack_names[:]:
            try:
                events = cloudformation.describe_stack_events(StackName=stack_name)['StackEvents']
            except cloudformation.exceptions.ClientError as exc:
                if 'does not exist' in exc.response['Error']['Message']:
                    stack_names.remove(stack_name)
            else:
                # Check events
                for e in events:
                    if not last_update.get(stack_name) or last_update.get(stack_name) < e['Timestamp']:
                        line = '\t'.join( list( dict.fromkeys([
                            e['Timestamp'].strftime("%H:%M:%S"),
                            stack_name,
                            e['LogicalResourceId'],
                            e['ResourceStatus'],
                            e.get('ResourceStatusReason',''),
                        ])))
                        if '_COMPLETE' in line: color = GREEN
                        elif '_IN_PROGRESS' in line: color = ''
                        elif '_FAILED' in line or 'failed to create' in line: color = RED
                        else: color = ''
                        logger.info(f'{color}{line}{END}')
                        last_update[stack_name] = e['Timestamp']
            try:
                current_stack = cloudformation.describe_stacks(StackName=stack_name)['Stacks'][0]
                if 'IN_PROGRESS' in current_stack['StackStatus']:
                    in_progress = True
            except:
                pass

            try:
                # Check nested stacks
                for res in cloudformation.list_stack_resources(StackName=stack_name)['StackResourceSummaries']:
                    if res['ResourceType'] == 'AWS::CloudFormation::Stack':
                        name = res['PhysicalResourceId'].split('/')[-2]
                        if name not in stack_names:
                            stack_names.append(name)
            except:
                pass

        if not stack_names or not in_progress: break
        time.sleep(5)

def initial_deploy_stacks():
    logger.info(f"account_id={account_id} region={boto3.session.Session().region_name}")
    create_options = dict(
        TimeoutInMinutes=60,
        Capabilities=['CAPABILITY_IAM','CAPABILITY_NAMED_IAM'],
        OnFailure='DELETE',
        EnableTerminationProtection=False,
        Tags=[ {'Key': 'branch', 'Value': 'branch'},],
        NotificationARNs=[],
    )
    try:
        cloudformation.create_stack(
            StackName='OptimizationManagementDataRoleStack',
            TemplateBody=open('static/Cost/300_Optimization_Data_Collection/Code/Management.yaml').read(),
            Parameters=[
                {'ParameterKey': 'CostAccountID',         'ParameterValue': account_id},
                {'ParameterKey': 'ManagementAccountRole', 'ParameterValue': "Lambda-Assume-Role-Management-Account"},
                {'ParameterKey': 'RolePrefix',            'ParameterValue': "WA-"},
            ],
            **create_options,
        )
    except cloudformation.exceptions.AlreadyExistsException:
        logger.info('OptimizationManagementDataRoleStack exists')

    try:
        cloudformation.create_stack(
            StackName='OptimizationDataRoleStack',
            TemplateBody=open('static/Cost/300_Optimization_Data_Collection/Code/optimisation_read_only_role.yaml').read(),
            Parameters=[
                {'ParameterKey': 'CostAccountID',                   'ParameterValue': account_id},
                {'ParameterKey': 'IncludeTransitGatewayModule',     'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeBudgetsModule',            'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeECSChargebackModule',      'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeInventoryCollectorModule', 'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeRDSUtilizationModule',     'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeRightsizingModule',        'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeTAModule',                 'ParameterValue': "yes"},
                {'ParameterKey': 'MultiAccountRoleName',            'ParameterValue': "Optimization-Data-Multi-Account-Role"},
                {'ParameterKey': 'RolePrefix',                      'ParameterValue': "WA-"},
            ],
            **create_options,
        )
    except cloudformation.exceptions.AlreadyExistsException:
        logger.info('OptimizationDataRoleStack exists')


    try:
        cloudformation.create_stack(
            StackName="OptimizationDataCollectionStack",
            TemplateBody=open('static/Cost/300_Optimization_Data_Collection/Code/Optimization_Data_Collector.yaml').read(),
            Parameters=[

                {'ParameterKey': 'CFNTemplateSourceBucket',         'ParameterValue': BUCKET},
                {'ParameterKey': 'ComputeOptimizerRegions',         'ParameterValue': "us-east-1,eu-west-1"},
                {'ParameterKey': 'DestinationBucket',               'ParameterValue': "costoptimizationdata"},
                {'ParameterKey': 'IncludeTransitGatewayModule',     'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeBudgetsModule',            'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeComputeOptimizerModule',   'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeECSChargebackModule',      'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeInventoryCollectorModule', 'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeOrgDataModule',            'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeRDSUtilizationModule',     'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeRightsizingModule',        'ParameterValue': "yes"},
                {'ParameterKey': 'IncludeTAModule',                 'ParameterValue': "yes"},
                {'ParameterKey': 'ManagementAccountID',             'ParameterValue': account_id},
                {'ParameterKey': 'ManagementAccountRole',           'ParameterValue': "Lambda-Assume-Role-Management-Account"},
                {'ParameterKey': 'MultiAccountRoleName',            'ParameterValue': "Optimization-Data-Multi-Account-Role"},
                {'ParameterKey': 'RolePrefix',                      'ParameterValue': "WA-"},
            ],
            **create_options,
        )
    except cloudformation.exceptions.AlreadyExistsException:
        logger.info('OptimizationDataCollectionStack exists')
        pass

    logger.info('Waiting for stacks')
    watch_stacks([
        "OptimizationManagementDataRoleStack",
        "OptimizationDataRoleStack",
        "OptimizationDataCollectionStack",
    ])


def clean_bucket():
    try:
        logger.info('Empty the bucket')
        s3.Bucket(f"costoptimizationdata{account_id}").object_versions.delete()
    except Exception as exc:
        logger.exception(exc)

def trigger_update():
    main_stack_name = 'OptimizationDataCollectionStack'
    for name in [
        f'Accounts-Collector-Function-{main_stack_name}',
        f'pricing-Lambda-Function-{main_stack_name}',
        f'cost-explorer-rightsizing-{main_stack_name}',
        'WA-compute-optimizer-Trigger-Export',
        f'Organization-Data-{main_stack_name}',
        ]:
        logger.info('Invoking ' + name)
        response = boto3.client('lambda').invoke(FunctionName=name)
        stdout = response['Payload'].read().decode('utf-8')
        print(indent(stdout, ' ' * 4))

def setup():
    global start_time
    start_time = datetime.datetime.now()
    initial_deploy_stacks()
    #update_nested_stacks()
    clean_bucket()
    trigger_update()
    logger.info('Waiting 1 min')
    time.sleep(1 * 60)
    logger.info('and another 1 min')
    time.sleep(1 * 60)

# TODO: move to utils.py?
def athena_query(sql_query, sleep_duration=1, database: str=None, catalog: str='AwsDataCatalog', workgroup: str='primary'):
    """ Executes an AWS Athena Query and return dict"""
    context = {}
    if database: context['Database'] = database
    if catalog: context['Catalog'] = catalog
    response = athena.start_query_execution(
        QueryString=sql_query,
        QueryExecutionContext=context,
        WorkGroup=workgroup,
    )
    query_id = response.get('QueryExecutionId')
    current_status = athena.get_query_execution(QueryExecutionId=query_id)['QueryExecution']['Status']
    while current_status['State'] in ['SUBMITTED', 'RUNNING', 'QUEUED']:
        current_status = athena.get_query_execution(QueryExecutionId=query_id)['QueryExecution']['Status']
        time.sleep(sleep_duration)
    if current_status['State'] != "SUCCEEDED":
        failure_reason = current_status['StateChangeReason']
        logger.debug(f'Full query: {repr(sql_query)}')
        raise Exception('Athena query failed: {}'.format(failure_reason))
    results = athena.get_query_results(QueryExecutionId=query_id)
    if not results['ResultSet']['Rows']:
        return []
    keys = [r['VarCharValue'] for r in results['ResultSet']['Rows'][0]['Data']]
    return [ dict(zip(keys, [r.get('VarCharValue') for r in row['Data']])) for row in results['ResultSet']['Rows'][1:]]


def test_budgets_data():
    data = athena_query('SELECT * FROM "optimization_data"."budgets_data" LIMIT 10;')
    assert len(data) > 0, 'budgets_data is empty'

def test_cost_explorer_rightsizing_data():
    data = athena_query('SELECT * FROM "optimization_data"."cost_explorer_rightsizing_data" LIMIT 10;')
    assert len(data) > 0, 'cost_explorer_rightsizing_data is empty'

def test_ecs_chargeback_data():
    data = athena_query('SELECT * FROM "optimization_data"."ecs_chargeback_data" LIMIT 10;')
    assert len(data) > 0, 'ecs_chargeback_data is empty'

def test_inventory_ami_data():
    data = athena_query('SELECT * FROM "optimization_data"."inventory_ami_data" LIMIT 10;')
    assert len(data) > 0, 'inventory_ami_data is empty'

def test_inventory_ebs_data():
    data = athena_query('SELECT * FROM "optimization_data"."inventory_ebs_data" LIMIT 10;')
    assert len(data) > 0, 'inventory_ebs_data is empty'

def test_inventory_snapshot_data():
    data = athena_query('SELECT * FROM "optimization_data"."inventory_snapshot_data" LIMIT 10;')
    assert len(data) > 0, 'inventory_snapshot_data is empty'

def test_rds_usage_data():
    data = athena_query('SELECT * FROM "optimization_data"."rds_usage_data" LIMIT 10;')
    assert len(data) > 0, 'rds_usage_data is empty'

def test_trusted_advisor_data():
    data = athena_query('SELECT * FROM "optimization_data"."trusted_advisor_data" LIMIT 10;')
    assert len(data) > 0, 'trusted_advisor_data is empty'

def test_transit_gateway_data():
    data = athena_query('SELECT * FROM "optimization_data"."transit_gateway_data" LIMIT 10;')
    assert len(data) > 0, 'transit_gateway_data is empty'

def test_compute_optimizer_export_triggered():
    global start_time

    for region in ['us-east-1', 'eu-west-1']:
        co = boto3.client('compute-optimizer', region_name=region)
        jobs = co.describe_recommendation_export_jobs()['recommendationExportJobs']
        jobs_since_start = [job for job in jobs if job['creationTimestamp'].replace(tzinfo=None) > start_time.replace(tzinfo=None)]
        if len(jobs_since_start) < 5:
           logger.info(f'Jobs: {jobs_since_start}')
           raise Exception(f'Not all jobs launched {len(jobs_since_start)}, must be 5 in {region}')
        jobs_failed = [job for job in jobs_since_start if job.get('status') == 'failed']
        assert len(jobs_failed) == 0, f'Some jobs failed {jobs_failed}'
    # TODO: check how we can add better test, taking into account 15-30 mins delay of export in CO



def teardown():
    try:
        clean_bucket()
    except:
        pass
    for stack_name in [
        'OptimizationManagementDataRoleStack',
        'OptimizationDataRoleStack',
        'OptimizationDataCollectionStack',
        ]:
        try:
            cloudformation.delete_stack(StackName=stack_name)
            logger.info(f'deleting {stack_name} initiated')
        except cloudformation.exceptions.ClientError as exc:
            print (stack_name, exc.response)

    watch_stacks([
        'OptimizationManagementDataRoleStack',
        'OptimizationDataRoleStack',
        'OptimizationDataCollectionStack',
    ])

    logger.info('Deleting all athena tables in optimization_data')
    tables = athena.list_table_metadata(CatalogName='AwsDataCatalog', DatabaseName='optimization_data')['TableMetadataList']
    for t in tables:
        logger.info('Deleting ' + t["Name"])
        athena_query(f'DROP TABLE `{t["Name"]}`;', database='optimization_data')


def main():
    try:
        setup()
        for f in [
                test_budgets_data,
                test_cost_explorer_rightsizing_data,
                test_ecs_chargeback_data,
                test_inventory_ami_data,
                test_inventory_ebs_data,
                test_inventory_snapshot_data,
                test_rds_usage_data,
                test_trusted_advisor_data,
                test_transit_gateway_data,
                test_compute_optimizer_export_triggered,
            ]:
            try:
                logger.info('Testing ' +  f.__name__)
                f()
            except Exception as exc:
                logger.exception(exc)
                logger.error('Failed' + f.__name__)
            else:
                logger.info(f.__name__ +  ' ok')

    except Exception as exc:
        logger.exception(exc)
    finally:
        logger.info('Press Ctr-C to stop before teardown. 30s')
        time.sleep(30)
        logger.info('teardown')
        teardown()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    main()