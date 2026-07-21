"""Diagnostic: test that moto and the handler can be imported."""
import sys
import os

print("Python:", sys.version)
print("Checking moto import...")
from moto import mock_aws
print("moto OK")

print("Checking boto3 import...")
import boto3
print("boto3 OK")

print("Checking handler import...")
os.environ["AGGREGATION_INSTANCE_ID"] = "i-test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lambdas.aggregation_trigger.handler as mod
print("handler OK, INSTANCE_ID =", mod.INSTANCE_ID)

print("All imports succeeded.")
