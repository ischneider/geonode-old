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

from django.conf.urls.defaults import *

urlpatterns = patterns('geonode.silage.views',
    url(r'^$', 'search_page', name='search'),
    url(r'^api$', 'search_api', name='search_api'),
    url(r'^api/authors$', 'author_list', name='search_api_author_list'),
    
    url(r'^layers$', 'search_page', kwargs={'bytype':'layer'}, name='layer_search_page'),
    url(r'^maps$', 'search_page', kwargs={'bytype':'map'}, name='maps_search'),
)