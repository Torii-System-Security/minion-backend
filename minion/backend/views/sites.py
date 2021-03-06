#!/usr/bin/env python

import calendar
import datetime
import re
import uuid
from flask import jsonify, request
from celery.schedules import crontab_parser, ParseException

from minion.backend.app import app
import minion.backend.tasks as tasks
from minion.backend.views.base import _check_required_fields, api_guard, groups, sites, scanschedules, siteCredentials
from minion.backend.views.groups import _check_group_exists
from minion.backend.views.plans import _check_plan_exists

def _check_site_url(url):
    regex = re.compile(r"^((http|https)://(localhost|([a-z0-9][-a-z0-9]*)(\.[a-z0-9][-a-z0-9]*)+)(:\d+)?)"
                       r"|"
                       r"((([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])(\/(\d|[1-2]\d|3[0-2]))?)$")
    return regex.match(url) is not None

#def _check_required_fields(expected, fields):
#    for field in fields:
#        if field not in expected:
#            return False
#    return True

def _find_groups_for_site(site):
    """Find all the groups the site is part of"""
    return [g['name'] for g in groups.find({"sites":site})]

def sanitize_site(site):
    if '_id' in site:
        del site['_id']
    if 'created' in site:
        site['created'] = calendar.timegm(site['created'].utctimetuple())
    return site


def check_cron(crontab):
    cron_errors = []

    # Validate Minute
    try:
        crontab_parser(60).parse(crontab['minute'])
    except (ValueError, ParseException):
        cron_errors.append("Error in Value: Minute")

    # Validate Hour
    try:
        crontab_parser(24).parse(crontab['hour'])
    except (ValueError, ParseException):
        cron_errors.append("Error in Value: Hour")

    # Validate Day of Week
    try:
        crontab_parser(7).parse(crontab['day_of_week'])
    except (ValueError, ParseException):
        cron_errors.append("Error in Value: Day of Week")

    # Validate Day of Month
    try:
        crontab_parser(31,1).parse(crontab['day_of_month'])
    except (ValueError, ParseException):
        cron_errors.append("Error in Value: Day of Month")

    # Validate Month of Year
    try:
        crontab_parser(12,1).parse(crontab['month_of_year'])
    except (ValueError, ParseException):
        cron_errors.append("Error in Value: Month of Year")

    return cron_errors


# API Methods to manage sites

#
# Expects a site id to GET:
#
#  GET /sites/b263bdc6-8692-4ace-aa8b-922b9ec0fc37
#
# Returns the site record:
#
#  { 'success': True,
#    'site': { 'id': 'b263bdc6-8692-4ace-aa8b-922b9ec0fc37',
#              'url': 'https://www.mozilla.com',
#              'groups': ['mozilla', 'key-initiatives'] } }
#
# The groups list is not part of the site but is generated by querying the groups records.
#
# Or returns an error:
#
#  { 'success': False, 'reason': 'site-already-exists' }
#
#

@app.route('/sites/<site_id>', methods=['GET'])
@api_guard
def get_site(site_id):
    site = sites.find_one({'id': site_id})
    if not site:
        return jsonify(success=False, reason='no-such-site')
    site['groups'] = _find_groups_for_site(site['url'])
    return jsonify(success=True, site=sanitize_site(site))

#
# Expects a partially filled out site as POST data:
#
#  POST /sites
#
#  { 'url': 'https://www.mozilla.com',
#    'plans': ['basic', 'nmap'],
#    'groups': ['mozilla', 'key-initiatives'] }
#
# Returns the full site record including the generated id:
#
#  { 'success': True,
#    'site': { 'id': 'b263bdc6-8692-4ace-aa8b-922b9ec0fc37',
#              'url': 'https://www.mozilla.com',
#              'plans': ['basic', 'nmap'],
#              'groups': ['mozilla', 'key-initiatives'] } }
#
# Or returns an error:
#
#  { 'success': False, 'reason': 'site-already-exists' }
#  { 'success': False, 'reason': 'Group xyz does not exist' }
#

@app.route('/sites', methods=['POST'])
@api_guard('application/json')
def create_site():
    site = request.json
    # Verify incoming site: url must be valid, groups must exist, plans must exist
    if not _check_site_url(site.get('url')):
        return jsonify(success=False, reason='invalid-url')
    if not _check_required_fields(site, ['url']):
        return jsonify(success=False, reason='missing-required-field')
    for group in site.get('groups', []):
        if not _check_group_exists(group):
            return jsonify(success=False, reason='unknown-group')
    for plan_name in site.get('plans', []):
        if not _check_plan_exists(plan_name):
            return jsonify(success=False, reason='unknown-plan')
    if sites.find_one({'url': site['url']}) is not None:
        return jsonify(success=False, reason='site-already-exists')
    # Create the site
    new_site = { 'id': str(uuid.uuid4()),
                 'url':  site['url'],
                 'plans': site.get('plans', []),
                 'created': datetime.datetime.utcnow()}

    if site.get('verification',{}).get('enabled',False):
        new_site['verification'] = {'enabled': True, 'value': str(uuid.uuid4())}
    else:
        new_site['verification'] = {'enabled': False, 'value': None}

    sites.insert(new_site)
    # Add the site to the groups - group membership is stored in the group object, not in the site
    for group_name in site.get('groups', []):
        # No need to check if the site is already in the group as we just added the site
        groups.update({'name':group_name},{'$addToSet': {'sites': site['url']}})
    new_site['groups'] = site.get('groups', [])
    # Return the new site
    return jsonify(success=True, site=sanitize_site(new_site))

#
# Expects a partially filled out site as POST data. The site with the
# specified site_id (in the URL) will be updated.
#
# It is not possible to change the url. For that you need to delete the
# site and create a new one.
#
#  POST /sites/<site_id>
#
#  { 'url': 'https://www.mozilla.com',
#    'plans': ['basic', 'nmap'],
#    'groups': ['mozilla', 'key-initiatives'] }
#
# Returns the full site record including the generated id:
#
#  { 'success': True,
#    'site': { 'id': 'b263bdc6-8692-4ace-aa8b-922b9ec0fc37',
#              'url': 'https://www.mozilla.com',
#              'plans': ['basic', 'nmap'],
#              'groups': ['mozilla', 'key-initiatives'] } }
#
# Or returns an error:
#
#  { 'success': False, 'reason': 'no-such-site' }
#  { 'success': False, 'reason': 'unknown-group' }
#  { 'success': False, 'reason': 'unknown-plan' }
#

@app.route('/sites/<site_id>', methods=['POST'])
@api_guard
def update_site(site_id):
    new_site = request.json
    # Verify incoming site. It must exist, groups must exist, plans must exist.
    site = sites.find_one({'id': site_id})
    if not site:
        return jsonify(success=False, reason='no-such-site')
    site['groups'] = _find_groups_for_site(site['url'])
    for group in new_site.get('groups', []):
        if not _check_group_exists(group):
            return jsonify(success=False, reason='unknown-group')
    for plan_name in new_site.get('plans', []):
        if not _check_plan_exists(plan_name):
            return jsonify(success=False, reason='unknown-plan')
    if 'groups' in new_site:
        # Add new groups
        for group_name in new_site.get('groups', []):
            if group_name not in site['groups']:
                groups.update({'name':group_name},{'$addToSet': {'sites': site['url']}})
        # Remove old groups
        for group_name in site['groups']:
            if group_name not in new_site.get('groups', []):
                groups.update({'name':group_name},{'$pull': {'sites': site['url']}})

    if 'plans' in new_site:
        # Update the site. At this point we can only update plans.
        sites.update({'id': site_id}, {'$set': {'plans': new_site.get('plans')}})

    new_verification = new_site['verification']
    old_verification = site.get('verification')
    # if site doesn't have 'verification', do us a favor, update the document as it is outdated!
    if not old_verification or old_verification['enabled'] != new_verification['enabled']:
        # to make logic simpler, even if the new request wants to
        # disable verification, generate a new value anyway.
        sites.update({'id': site_id},
            {'$set': {
                 'verification': {
                    'enabled': new_verification['enabled'],
                    'value': str(uuid.uuid4())}}})

    # Return the updated site
    site = sites.find_one({'id': site_id})
    if not site:
        return jsonify(success=False, reason='no-such-site')
    site['groups'] = _find_groups_for_site(site['url'])
    return jsonify(success=True, site=sanitize_site(site))

#
# Returns a list of sites or return the site matches the query. Currently
# only url is supported.
#
#  GET /sites
#  GET /sites?url=http://www.mozilla.com
#
# Returns a list of sites found, even if there is one result:
#
#  [{ 'id': 'b263bdc6-8692-4ace-aa8b-922b9ec0fc37',
#     'url': 'https://www.mozilla.com',
#     'groups': ['mozilla', 'key-initiatives'] },
#    ...]
#

@app.route('/sites', methods=['GET'])
@api_guard
def get_sites():
    query = {}
    url = request.args.get('url')
    if url:
        query['url'] = url
    sitez = [sanitize_site(site) for site in sites.find(query)]
    for site in sitez:
        site['groups'] = _find_groups_for_site(site['url'])

    return jsonify(success=True, sites=sitez)


# Returns credential Info exept for password from siteCredentials collection
@app.route('/credInfo', methods=['GET'])
@api_guard
def get_credInfo():
    credInfo = {}
    for site in siteCredentials.find():

        authData = site['authData']
        data = {
            'site':site['site'],
            'plan':site['plan'],
            'authData': site['authData']
        }

        #remove password from the response
        data['authData']['password'] = ""
        if not site['site'] in credInfo:
            credInfo[site['site']] = {}

        credInfo[site['site']][site['plan']] = data


    return jsonify(success=True, credInfo=credInfo)


# Sets siteCredentials
@app.route('/setCredentials', methods=["POST"])
def setCredentials():
    cred_data = request.json
    site = cred_data.get('site')
    plan = cred_data.get('plan')
    authData = cred_data.get('authData')

    data = {
        'site':site,
        'plan':plan,
        'authData':authData
    }

    if authData.get('remove'):
        siteCredentials.remove({'site':site, 'plan':plan})
        return jsonify(message="Removed Site Credentials", success=True)


    # Insert/Update credentials
    siteCreds = siteCredentials.find_one({"site":site, "plan":plan})
    if not siteCreds:
      siteCredentials.insert(data)
    else:
      #Update everything else except password unless requested
      updatedAuthData = {
        'authData.method':authData.get('method'),
        'authData.url': authData.get('url'),
        'authData.email': authData.get('email'),
        'authData.before_login_element_xpath': authData.get('before_login_element_xpath'),
        'authData.login_button_xpath': authData.get('login_button_xpath'),
        'authData.login_script': authData.get('login_script'),
        'authData.after_login_element_xpath': authData.get('after_login_element_xpath'),
        'authData.username': authData.get('username'),
        'authData.username_field_xpath' : authData.get('username_field_xpath'),
        'authData.password_field_xpath' : authData.get('password_field_xpath'),
        'authData.expected_cookies' : authData.get('expected_cookies')
      }

      # If password is non-blank, update it
      password = authData.get('password')
      if password:
        updatedAuthData['authData.password'] = password


      siteCredentials.update({"site":site, "plan":plan},
                       {"$set": updatedAuthData});

    return jsonify(message="Added Site Credentials", success=True)


@app.route('/scanschedule', methods=["POST"])
def scanschedule():
    site = request.json
    scan_id = site.get('scan_id')
    schedule = site.get('schedule')

    plan = site.get('plan')
    target = site.get('target')

    removeSite = schedule.get('remove')
    enabled = True
    crontab = {}
    message = "Scan Schedule not set"

    if removeSite is not None:
        # Removing scan from scanschedule results in incomplete removal because of celerybeat-mongo running in background
        # Hence  we just set "enabled" to false
        enabled = False
        message = "Removed Schedule for: " + target

    else:
        enabled = True
        message="Scheduled Scan successfully set for site: " + target

    crontab = {
      'minute':str(schedule.get('minute')),
      'hour':str(schedule.get('hour')),
      'day_of_week':str(schedule.get('dayOfWeek')),
      'day_of_month':str(schedule.get('dayOfMonth')),
      'month_of_year':str(schedule.get('monthOfYear'))
    }

    # Validate Crontab schedule values
    crontab_errors = check_cron(crontab)
    if crontab_errors:
        message = "Error in crontab values"
        return jsonify(message=message,success=False,errors=crontab_errors)

    data = {
      '_cls': 'PeriodicTask', # https://github.com/zmap/celerybeat-mongo:  because Mongoengine the database, objects must have a field _cls set to PeriodicTask 
      'task': "minion.backend.tasks.run_scheduled_scan",
      'args': [target, plan],
      'site': target,
      'queue':'scanschedule',
      'routing_key':'scanschedule',
      'exchange':'', #Exchange is not required. Fails sometimes if exchange is provided. #TODO Figure out why
      'plan': plan,
      'name': target + ":" + plan,
      'enabled': enabled,
      'crontab': crontab
    }

    # Insert/Update existing schedule by target and plan
    schedule = scanschedules.find_one({"site":target, "plan":plan})
    if not schedule:
      scanschedules.insert(data)
    else:
      scanschedules.update({"site":target, "plan":plan},
                       {"$set": {"crontab": crontab, "enabled":enabled}});


    return jsonify(message=message,success=True)
