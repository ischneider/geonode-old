#########################################################################
#
# Copyright (C) 2012 OpenPlans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

from django import forms
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils import simplejson as json

from geonode.layers.models import Layer
from geonode.security.models import filter_security

def index(request, template='index.html'):
    from django.db import connection
    if False:
        q = filter_security(Layer.objects.all(), request.user, Layer, 'view_layer')
        page = int(request.REQUEST.get('page', 0))
        ctx = {'object_list' : q, 'test_paginate' : True}
        if page > 0:
            r = render_to_response('geonode/paginate_content.html', RequestContext(request, ctx))
        else:
            ctx['total'] = q.count()
            r = render_to_response(template, RequestContext(request, ctx))
    else:
        from geonode.search.views import search_page
        post = request.POST.copy()
        post.update({'type': 'layer'})
        request.POST = post
        r = search_page(request, template=template)
    print 'queries', len(connection.queries)
    return r

class AjaxLoginForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput)
    username = forms.CharField()

def ajax_login(request):
    if request.method != 'POST':
        return HttpResponse(
                content="ajax login requires HTTP POST",
                status=405,
                mimetype="text/plain"
            )
    form = AjaxLoginForm(data=request.POST)
    if form.is_valid():
        username = form.cleaned_data['username']
        password = form.cleaned_data['password']
        user = authenticate(username=username, password=password)
        if user is None or not user.is_active:
            return HttpResponse(
                    content="bad credentials or disabled user",
                    status=400,
                    mimetype="text/plain"
                )
        else:
            login(request, user)
            if request.session.test_cookie_worked():
                request.session.delete_test_cookie()
            return HttpResponse(
                    content="successful login",
                    status=200,
                    mimetype="text/plain"
                )
    else:
        return HttpResponse(
                "The form you submitted doesn't look like a username/password combo.",
                mimetype="text/plain",
                status=400
            )

def ajax_lookup(request):
    if request.method != 'POST':
        return HttpResponse(
            content='ajax user lookup requires HTTP POST',
            status=405,
            mimetype='text/plain'
        )
    elif 'query' not in request.POST:
        return HttpResponse(
            content='use a field named "query" to specify a prefix to filter usernames',
            mimetype='text/plain'
        )
    users = User.objects.filter(username__startswith=request.POST['query'])
    json_dict = {
        'users': [({'username': u.username}) for u in users],
        'count': users.count(),
    }
    return HttpResponse(
        content=json.dumps(json_dict),
        mimetype='text/plain'
    )
