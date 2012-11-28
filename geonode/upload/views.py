"""
Provide views for doing an upload.

The upload process may be multi step so views are all handled internally here
by the view function.

The pattern to support separation of view/logic is each step in the upload
process is suffixed with "_step". The view for that step is suffixed with
"_step_view". The goal of seperation of view/logic is to support various
programatic uses of this API. The logic steps should not accept request objects
or return response objects.

State is stored in a UploaderSession object stored in the user's session.
This needs to be made more stateful by adding a model.
"""
from geonode.layers.forms import NewLayerUploadForm
from geonode.utils import json_response
from geonode.upload import forms
from geonode.upload.models import Upload, UploadFile
from geonode.upload import upload
from geonode.upload.utils import rename_and_prepare, find_sld, get_upload_type
from geonode.upload.forms import UploadFileForm 

from geonode.geoserver.uploader import uploader

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.utils.html import escape
from django.core.urlresolvers import reverse
from django.shortcuts import get_object_or_404
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.contrib.auth.decorators import login_required
from django.views.generic import CreateView, DeleteView

import json
import os
import logging
import uuid

logger = logging.getLogger(__name__)

_SESSION_KEY = 'geonode_upload_session'
_ALLOW_TIME_STEP = hasattr(settings, "UPLOADER_SHOW_TIME_STEP") and settings.UPLOADER_SHOW_TIME_STEP or False
_ASYNC_UPLOAD = settings.DB_DATASTORE == True

# at the moment, the various time support transformations require the database
if _ALLOW_TIME_STEP and not _ASYNC_UPLOAD:
    raise Exception("To support the time step, you must enable DB_DATASTORE")


def _is_async_step(upload_session):
    return _ASYNC_UPLOAD and get_next_step(upload_session, offset=2) == 'run'


def _progress_redirect(step):
    return json_response(dict(
        success = True,
        redirect_to= reverse('data_upload', args=[step]),
        progress = reverse('data_upload_progress')
    ))


def _error_response(req, exception=None, errors=None, force_ajax=False):
    if exception:
        logger.exception('Unexpected error in upload step')
    else:
        logger.warning('upload error: %s', errors)
    if req.is_ajax() or force_ajax:
        content_type = 'text/html' if not req.is_ajax() else None
        return json_response(exception=exception, errors=errors,
                             content_type=content_type)
    # not sure if any responses will (ideally) ever be non-ajax
    if errors:
        exception = "<br>".join(errors)
    return render_to_response('upload/upload_error.html', RequestContext(req,{
        'error_msg' : 'Unexpected error : %s,' % exception
    }))


def _next_step_response(req, upload_session, force_ajax=False):
    # if the current step is the view POST for this step, advance one
    if req.method == 'POST':
        if upload_session.completed_step:
            advance_step(req, upload_session)
        else:
            upload_session.completed_step = 'save'

    next = get_next_step(upload_session)

    if next == 'time':
        # @TODO we skip time steps for coverages currently
        import_session = upload_session.import_session
        feature_type = import_session.tasks[0].items[0].resource
        if feature_type.resource_type == 'coverage':
            upload_session.completed_step = 'time'
            return _next_step_response(req, upload_session)

    # @todo this is not handled cleanly - run is not a real step in that it
    # has no corresponding view served by the 'view' function.
    if next == 'run':
        upload_session.completed_step = next
        if _ASYNC_UPLOAD and req.is_ajax():
            return run_response(req, upload_session)
        else:
            # on sync we want to run the import and advance to the next step
            run_import(upload_session, async=False)
            return _next_step_response(req, upload_session,
                                       force_ajax=force_ajax)
    if req.is_ajax() or force_ajax:
        content_type = 'text/html' if not req.is_ajax() else None
        return json_response(redirect_to=reverse('data_upload', args=[next]),
                             content_type=content_type)
    return HttpResponseRedirect(reverse('data_upload', args=[next]))
    

def _create_time_form(import_session, form_data):
    feature_type = import_session.tasks[0].items[0].resource
    filter_type = lambda b : [ att.name for att in feature_type.attributes if att.binding == b]

    args = dict(
        time_names=filter_type('java.util.Date'),
        text_names=filter_type('java.lang.String'),
        year_names=filter_type('java.lang.Integer') +
          filter_type('java.lang.Long') +
          filter_type('java.lang.Double')
    )
    if form_data:
        return forms.TimeForm(form_data, **args)
    return forms.TimeForm(**args)


def save_step_view(req, session):
    if req.method == 'GET':
        s = os.statvfs('/')
        mb = s.f_bsize * s.f_bavail / (1024. * 1024)
        return render_to_response('upload/layer_upload.html',
            RequestContext(req, {
            'storage_remaining': "%d MB" % mb,
            'enough_storage': mb > 64,
            'async_upload' : _ASYNC_UPLOAD,
            'incomplete' : Upload.objects.get_incomplete_uploads(req.user)
        }))
        
    assert session is None

    form = NewLayerUploadForm(req.POST, req.FILES)
    tempdir = None
    if form.is_valid():
        tempdir, base_file = form.write_files()
        base_file = rename_and_prepare(base_file)
        name, ext = os.path.splitext(os.path.basename(base_file))
        import_session = upload.save_step(req.user, name, base_file, overwrite=False)
        sld = find_sld(base_file)
        logger.info('provided sld is %s' % sld)
        upload_type = get_upload_type(base_file)
        upload_session = req.session[_SESSION_KEY] = upload.UploaderSession(
            tempdir=tempdir,
            base_file=base_file,
            name=name,
            import_session=import_session,
            layer_abstract=form.cleaned_data["abstract"],
            layer_title=form.cleaned_data["layer_title"],
            permissions=form.cleaned_data["permissions"],
            import_sld_file = sld,
            upload_type = upload_type
        )
        return _next_step_response(req, upload_session, force_ajax=True)
    else:
        errors = []
        for e in form.errors.values():
            errors.extend([escape(v) for v in e])
        return _error_response(req, errors=errors)


def data_upload_progress(req):
    """This would not be needed if geoserver REST did not require admin role
    and is an inefficient way of getting this information"""
    upload_session = req.session[_SESSION_KEY]
    import_session = upload_session.import_session
    progress = import_session.tasks[0].items[0].get_progress()
    return json_response(progress)


def srs_step_view(req, upload_session):
    import_session = upload_session.import_session

    form = None
    if req.method == 'POST':
        form = forms.SRSForm(req.POST)
        if form.is_valid():
            srs = form.cleaned_data['srs']
            upload.srs_step(upload_session, srs)
            return _next_step_response(req, upload_session)

    if import_session.tasks[0].state == 'INCOMPLETE':
        # CRS missing/unknown
        if import_session.tasks[0].items[0].state == 'NO_CRS':
            native_crs = import_session.tasks[0].items[0].resource.nativeCRS
            form = form or forms.SRSForm()

    if form:
        name = import_session.tasks[0].items[0].layer.name
        return render_to_response('upload/layer_upload_crs.html',
                                  RequestContext(req,{
                                        'native_crs' : native_crs,
                                        'form' : form,
                                        'layer_name' : name
                                  }))
    # mark this completed since there is no post-back when skipping
    upload_session.completed_step = 'srs'                              
    return _next_step_response(req, upload_session)


latitude_names = set(['latitude', 'lat'])
longitude_names = set(['longitude', 'lon', 'lng', 'long'])


def is_latitude(colname):
    return colname.lower() in latitude_names


def is_longitude(colname):
    return colname.lower() in longitude_names


def csv_step_view(request, upload_session):
    import_session = upload_session.import_session
    item = import_session.tasks[0].items[0]
    feature_type = item.resource
    attributes = feature_type.attributes

    # need to check if geometry is found
    # if so, can proceed directly to next step
    for attr in attributes:
        if attr.binding == u'com.vividsolutions.jts.geom.Point':
            upload_session.completed_step = 'csv'
            return _next_step_response(request, upload_session)

    # no geometry found, let's find all the numerical columns
    number_names = ['java.lang.Integer', 'java.lang.Double']
    point_candidates = [attr.name for attr in attributes
                        if attr.binding in number_names]
    point_candidates.sort()

    # form errors to display to user
    error = None

    lat_field = request.POST.get('lat', '')
    lng_field = request.POST.get('lng', '')

    if request.method == 'POST':
        if not lat_field or not lng_field:
            error = 'Missing latitude/longitude fields'
        elif (lat_field not in point_candidates
              or lng_field not in point_candidates):
            error = 'Invalid latitude/longitude fields'
        elif lat_field == lng_field:
            error = 'Cannot choose same column for latitude and longitude'
        if not error:
            upload.csv_step(upload_session, lat_field, lng_field)
            return _next_step_response(request, upload_session)
    # try to guess the lat/lng fields from the candidates
    lat_candidate = None
    lng_candidate = None
    for candidate in attributes:
        if candidate.name in point_candidates:
            if is_latitude(candidate.name):
                lat_candidate = candidate.name
            elif is_longitude(candidate.name):
                lng_candidate = candidate.name
    if request.method == 'POST':
        guessed_lat_or_lng = False
        selected_lat = lat_field
        selected_lng = lng_field
    else:
        guessed_lat_or_lng = bool(lat_candidate or lng_candidate)
        selected_lat = lat_candidate
        selected_lng = lng_candidate
    present_choices = len(point_candidates) >= 2
    context = dict(present_choices=present_choices,
                   point_candidates=point_candidates,
                   async_upload=_is_async_step(upload_session),
                   selected_lat=selected_lat,
                   selected_lng=selected_lng,
                   guessed_lat_or_lng=guessed_lat_or_lng,
                   layer_name = import_session.tasks[0].items[0].layer.name,
                   error = error,
                   )
    return render_to_response('upload/layer_upload_csv.html',
                              RequestContext(request, context))


def time_step_view(request, upload_session):
    import_session = upload_session.import_session

    if request.method == 'GET':
        # check for invalid attribute names
        feature_type = import_session.tasks[0].items[0].resource
        if feature_type.resource_type == 'featureType':
            invalid = filter(lambda a: a.name.find(' ') >= 0, feature_type.attributes)
            if invalid:
                att_list = "<pre>%s</pre>" % '. '.join([a.name for a in invalid])
                msg = "Attributes with spaces are not supported : %s" % att_list
                return render_to_response('upload/upload_error.html', RequestContext(request,{
                    'error_msg' : msg
                }))
        context = {
            'time_form': _create_time_form(import_session, None),
            'layer_name': import_session.tasks[0].items[0].layer.name,
            'async_upload' : _is_async_step(upload_session)
        }
        return render_to_response('upload/layer_upload_time.html',
                                  RequestContext(request, context))

    elif request.method != 'POST':
        raise Exception()

    form = _create_time_form(import_session, request.POST)
    #@todo validation feedback, though we shouldn't get here
    if not form.is_valid():
        logger.warning('Invalid upload form: %s', form.errors)
        return _error_response(request, errors=["Invalid Submission"])

    cleaned = form.cleaned_data

    time_attribute, time_transform_type = None, None
    end_time_attribute, end_time_transform_type = None, None

    field_collectors = [
        ('time_attribute', None),
        ('text_attribute', 'DateFormatTransform'),
        ('year_attribute', 'IntegerFieldToDateTransform')
    ]

    for field, transform_type in field_collectors:
        time_attribute = cleaned.get(field, None)
        if time_attribute:
            time_transform_type = transform_type
            break
    for field, transform_type in field_collectors:
        end_time_attribute = cleaned.get('end_' + field, None)
        if end_time_attribute:
            end_time_transform_type = transform_type
            break

    if time_attribute:
        upload.time_step(
            upload_session,
            time_attribute=time_attribute,
            time_transform_type=time_transform_type,
            time_format=cleaned.get('text_attribute_format', None),
            end_time_attribute=end_time_attribute,
            end_time_transform_type=end_time_transform_type,
            end_time_format=cleaned.get('end_text_attribute_format', None),
            presentation_strategy=cleaned['presentation_strategy'],
            precision_value=cleaned['precision_value'],
            precision_step=cleaned['precision_step'],
        )

    return _next_step_response(request, upload_session)


def run_import(upload_session, async=_ASYNC_UPLOAD):
    # run_import can raise an exception which callers should handle
    target = upload.run_import(upload_session, async)
    upload_session.set_target(target)


def run_response(req, upload_session):
    run_import(upload_session)

    if _ASYNC_UPLOAD:
        next = get_next_step(upload_session)
        return _progress_redirect(next)
        
    return _next_step_response(req, upload_session)


def final_step_view(req, upload_session):
    del req.session[_SESSION_KEY]
    saved_layer = upload.final_step(upload_session, req.user)
    return HttpResponseRedirect(saved_layer.get_absolute_url())


_steps = {
    'save': save_step_view,
    'time': time_step_view,
    'srs' : srs_step_view,
    'final': final_step_view,
    'csv': csv_step_view,
}

# note 'run' is not a "real" step, but handled as a special case
# and 'save' is the implied first step :P
_pages = {
    'shp' : ('srs', 'time', 'run', 'final'),
    'tif' : ('time', 'run', 'final'),
    'kml' : ('run', 'final'),
    'csv' : ('csv', 'time', 'run', 'final'),
}

if not _ALLOW_TIME_STEP:
    for t, steps in _pages.items():
        steps = list(steps)
        if 'time' in steps:
            steps.remove('time')
        _pages[t] = tuple(steps)

def get_next_step(upload_session, offset = 1):
    assert upload_session.upload_type is not None
    try:
        pages = _pages[upload_session.upload_type]
    except KeyError, e:
        raise Exception('Unsupported file type: %s' % e.message)
    index = -1
    if upload_session.completed_step and upload_session.completed_step != 'save':
        index = pages.index(upload_session.completed_step)
    return pages[max(min(len(pages) - 1,index + offset),0)]


def get_previous_step(upload_session, post_to):
    pages = _pages[upload_session.upload_type]
    index = pages.index(post_to) - 1
    if index < 0: return 'save'
    return pages[index]


def advance_step(req, upload_session):
    upload_session.completed_step = get_next_step(upload_session)


@login_required
def view(req, step):
    """Main uploader view"""

    upload_session = None

    if step is None:
        if 'id' in req.GET:
            # upload recovery
            upload_obj = get_object_or_404(Upload, import_id=req.GET['id'], user=req.user)
            session = upload_obj.get_session()
            if session:
                req.session[_SESSION_KEY] = session
                return _next_step_response(req, session)
        
        step = 'save'

        # delete existing session
        if _SESSION_KEY in req.session:
            del req.session[_SESSION_KEY]

    else:
        if not _SESSION_KEY in req.session:
            return render_to_response("upload/no_upload.html", RequestContext(req,{}))
        upload_session = req.session[_SESSION_KEY]

    try:
        if req.method == 'GET' and upload_session:
            # set the current step to match the requested page - this
            # could happen if the form is ajax w/ progress monitoring as
            # the advance would have already happened @hacky
            upload_session.completed_step = get_previous_step(upload_session, step)

        resp = _steps[step](req, upload_session)
        # must be put back to update object in session
        if upload_session:
            req.session[_SESSION_KEY] = upload_session
        elif _SESSION_KEY in req.session:
            upload_session = req.session[_SESSION_KEY]
        if upload_session:
            Upload.objects.update_from_session(upload_session)
        return resp
    except upload.UploadException, e:
        return _error_response(req, errors=e.args)
    except uploader.BadRequest, e:
        return _error_response(req, errors=e.args)
    except Exception, e:
        if upload_session:
            # @todo probably don't want to do this
            upload_session.cleanup()
        code = uuid.uuid4()
        errors= ['Unexpected Error:','Please report the following code: %s' % code]
        return _error_response(req, exception=e, errors=errors)


@login_required
def delete(req, id):
    upload = get_object_or_404(Upload, import_id=id)
    if req.user != upload.user:
        raise PermissionDenied()
    upload.delete()
    return HttpResponse(reverse('data_upload_new'))


class UploadFileCreateView(CreateView):
    form_class = UploadFileForm
    model = UploadFile

    def form_valid(self, form):
        self.object = form.save()
        f = self.request.FILES.get('file')
        data = [{'name': f.name, 'url': settings.MEDIA_URL + "uploads/" + f.name.replace(" ", "_"), 'thumbnail_url': settings.MEDIA_URL + "pictures/" + f.name.replace(" ", "_"), 'delete_url': reverse('data_upload_remove', args=[self.object.id]), 'delete_type': "DELETE"}]
        response = JSONResponse(data, {}, response_mimetype(self.request))
        response['Content-Disposition'] = 'inline; filename=files.json'
        return response
    
    def form_invalid(self, form):
        data = [{}]
        response = JSONResponse(data, {}, response_mimetype(self.request))
        response['Content-Disposition'] = 'inline; filename=files.json'
        return response


def response_mimetype(request):
    if "application/json" in request.META['HTTP_ACCEPT']:
        return "application/json"
    else:
        return "text/plain"

class UploadFileDeleteView(DeleteView):
    model = UploadFile

    def delete(self, request, *args, **kwargs):
        """
        This does not actually delete the file, only the database record.  But
        that is easy to implement.
        """
        self.object = self.get_object()
        self.object.delete()
        if request.is_ajax():
            response = JSONResponse(True, {}, response_mimetype(self.request))
            response['Content-Disposition'] = 'inline; filename=files.json'
            return response
        else:
            return HttpResponseRedirect(reverse('data_upload_new'))


class JSONResponse(HttpResponse):
    """JSON response class."""
    def __init__(self,obj='',json_opts={},mimetype="application/json",*args,**kwargs):
        content = json.dumps(obj,**json_opts)
        super(JSONResponse,self).__init__(content,mimetype,*args,**kwargs)