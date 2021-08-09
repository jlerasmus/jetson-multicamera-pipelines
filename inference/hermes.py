import sys

from threading import Thread

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst

sys.path.append("../")
from common.is_aarch_64 import is_aarch64
from common.bus_call import bus_call

import pyds
import logging  # TODO: print -> logging


def exit_if_none(element):
    if element is None:
        sys.stderr.write(f"Pipeline element is None!\n Exiting.")
        sys.exit(1)
        return False
    else:
        return True


def _make_element_safe(el_type: str) -> Gst.Element:
    """
    Creates a gstremer element using el_type factory.
    Returns Gst.Element or throws an error if we fail.
    This is to avoid `None` elements in our pipeline
    """

    # name=None parameter asks Gstreamer to uniquely name the elements for us
    el = Gst.ElementFactory.make(el_type, name=None)

    if el is not None:
        return el
    else:
        print(f"Pipeline element is None!\n Exiting.")
        # TODO: narrow down the error
        # TODO: use Gst.ElementFactory.find to generate a more informative error message
        raise Error


class MultiCamPipeline(Thread):
    def __init__(self, *args, **kwargs):

        super().__init__()

        self._gobj_init = GObject.threads_init()
        Gst.init(None)

        # create an event loop and feed gstreamer bus mesages to it
        self._mainloop = GObject.MainLoop()

        # gst pipeline object
        self._p = self._create_pipeline()

        self._bus = self._p.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", bus_call, self._mainloop)

        self._pipeline = self._p

    def run(self):
        # start play back and listen to events
        print("Starting pipeline...")

        self._p.set_state(Gst.State.PLAYING)
        try:
            self._mainloop.run()
        except Exception as e:
            print(e)
        finally:
            self._p.set_state(Gst.State.NULL)

    def _create_pipeline(self):

        pipeline = Gst.Pipeline()
        exit_if_none(pipeline)

        print("Creating Sources...")
        source_0 = _make_element_safe("nvarguscamerasrc")
        source_1 = Gst.ElementFactory.make("nvarguscamerasrc", "camera-source-1")
        # source_2 = Gst.ElementFactory.make("nvarguscamerasrc", "camera-source-2")\
        sources = [source_0, source_1]

        print("Configuring Sources...")
        for idx, source in enumerate(sources):
            source.set_property("sensor-id", idx)
            source.set_property("bufapi-version", 1)

        print("Creating Pipeline Elements...")
        streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
        pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
        nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
        nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
        transform = Gst.ElementFactory.make(
            "nvegltransform", "nvegl-transform"
        )  # nvgltransform needed on aarch64
        sink = Gst.ElementFactory.make("fakesink", "fakesink")

        elements = [*sources, streammux, pgie, nvvidconv, nvosd, transform, sink]

        for el in elements:
            # Make sure we successfuly created all elements
            exit_if_none(el)

        print("Configuring Pipeline Elements...")
        streammux.set_property("width", 1920)
        streammux.set_property("height", 1080)
        streammux.set_property("batch-size", 3)
        streammux.set_property("batched-push-timeout", 4000000)
        pgie.set_property("config-file-path", "dstest1_pgie_config.txt")

        print("Adding elements to Pipeline...")
        for el in elements:
            pipeline.add(el)

        print("Linking elements in the Pipeline...")
        srcpads = []
        for idx, source in enumerate(sources):
            srcpad = source.get_static_pad("src")
            exit_if_none(srcpad)
            srcpads.append(srcpad)

        sinkpads = []
        for idx, source in enumerate(sources):
            sinkpad = streammux.get_request_pad(f"sink_{idx}")
            exit_if_none(sinkpad)
            sinkpads.append(sinkpad)

        for srcpad, sinkpad in zip(srcpads, sinkpads):
            srcpad.link(sinkpad)

        streammux.link(pgie)
        pgie.link(nvvidconv)
        nvvidconv.link(nvosd)
        nvosd.link(sink)
        nvosd.link(transform)
        transform.link(sink)

        # Lets add probe to get informed of the meta data generated, we add probe to
        # the sink pad of the osd element, since by that time, the buffer would have
        # had got all the metadata.
        osdsinkpad = nvosd.get_static_pad("sink")
        exit_if_none(osdsinkpad)
        osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, 0)

        return pipeline


PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3


def osd_sink_pad_buffer_probe(pad, info, u_data):
    print("OSD cb")
    frame_number = 0
    # Intiallizing object counter with 0.
    obj_counter = {
        PGIE_CLASS_ID_VEHICLE: 0,
        PGIE_CLASS_ID_PERSON: 0,
        PGIE_CLASS_ID_BICYCLE: 0,
        PGIE_CLASS_ID_ROADSIGN: 0,
    }
    num_rects = 0

    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
            # The casting is done by pyds.glist_get_nvds_frame_meta()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone.
            # frame_meta = pyds.glist_get_nvds_frame_meta(l_frame.data)
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num
        num_rects = frame_meta.num_obj_meta
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                # Casting l_obj.data to pyds.NvDsObjectMeta
                # obj_meta=pyds.glist_get_nvds_object_meta(l_obj.data)
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            obj_counter[obj_meta.class_id] += 1
            obj_meta.rect_params.border_color.set(0.0, 0.0, 1.0, 0.0)
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # Acquiring a display meta object. The memory ownership remains in
        # the C code so downstream plugins can still access it. Otherwise
        # the garbage collector will claim it when this probe function exits.
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        py_nvosd_text_params = display_meta.text_params[0]
        # Setting display text to be shown on screen
        # Note that the pyds module allocates a buffer for the string, and the
        # memory will not be claimed by the garbage collector.
        # Reading the display_text field here will return the C address of the
        # allocated string. Use pyds.get_string() to get the string content.
        py_nvosd_text_params.display_text = "Frame Number={} Number of Objects={} Vehicle_count={} Person_count={}".format(
            frame_number,
            num_rects,
            obj_counter[PGIE_CLASS_ID_VEHICLE],
            obj_counter[PGIE_CLASS_ID_PERSON],
        )

        # Now set the offsets where the string should appear
        py_nvosd_text_params.x_offset = 10
        py_nvosd_text_params.y_offset = 12

        # Font , font-color and font-size
        py_nvosd_text_params.font_params.font_name = "Serif"
        py_nvosd_text_params.font_params.font_size = 10
        # set(red, green, blue, alpha); set to White
        py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

        # Text background color
        py_nvosd_text_params.set_bg_clr = 1
        # set(red, green, blue, alpha); set to Black
        py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)
        # Using pyds.get_string() to get display_text as string
        print(pyds.get_string(py_nvosd_text_params.display_text))
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


if __name__ == "__main__":

    hermes = MultiCamPipeline()
    hermes.start()
