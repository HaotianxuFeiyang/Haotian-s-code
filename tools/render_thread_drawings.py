"""Render the unchanged production STEP files and explicit screw callouts in FreeCAD.

Open and execute the adjacent render_thread_drawings.FCMacro in FreeCAD.
Requires FreeCAD GUI, Part and built-in Qt.
No native IPT, third-party Python packages or externally installed fonts are needed.
Thread designations are explicit source metadata, never guessed from bore diameter.
"""
import hashlib
import json
import math
from pathlib import Path

import FreeCAD as App
import FreeCADGui as Gui
import Part
from PySide import QtCore, QtGui
from pivy import coin  # Registers camera-node wrappers used by FreeCAD's viewer.


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vec(values):
    return App.Vector(*values)


def unit(value):
    return value / value.Length


def font(size, bold=False):
    f = QtGui.QFont('Segoe UI')
    f.setPixelSize(size)
    f.setBold(bold)
    return f


def render(repo):
    production = repo / 'production'
    data = json.loads((production / 'drawing_holes.json').read_text(encoding='utf-8-sig'))
    records = []
    old_document = App.ActiveDocument
    for spec in data['parts']:
        source = production / (spec['stem'] + '.step')
        assert sha(source) == spec['step_sha256'], 'STEP changed: review hole metadata first'
        shape = Part.read(str(source))
        assert shape.isValid() and len(shape.Solids) == 1
        cylinders = []
        for face in shape.Faces:
            s = face.Surface
            if isinstance(s, Part.Cylinder):
                cylinders.append(s)
        checks = []
        for h in spec['holes']:
            point, axis = vec(h['point_mm']), unit(vec(h['inward_axis']))
            matches = [c for c in cylinders
                       if abs(c.Radius * 2 - h['model_bore_diameter_mm']) < 0.002
                       and abs(abs(unit(c.Axis).dot(axis)) - 1) < 1e-6
                       and (point - c.Center).cross(unit(c.Axis)).Length < 0.002]
            assert matches, 'Hole metadata does not match STEP: ' + spec['stem'] + '/' + h['id']
            checks.append({'id': h['id'], 'specification': h['spec'], 'tap': h['tap'],
                           'matching_cylinders': len(matches)})
        doc = App.newDocument('ThreadDrawing')
        try:
            obj = doc.addObject('PartDesign::Feature', 'ManufacturingPart')
            obj.Shape = shape
            obj.ViewObject.ShapeColor = (0.68, 0.73, 0.78)
            obj.ViewObject.LineColor = (0.12, 0.15, 0.18)
            obj.ViewObject.DisplayMode = 'Flat Lines'
            obj.ViewObject.LineWidth = 1.4
            obj.ViewObject.AngularDeflection = 5.0
            obj.ViewObject.Deviation = 0.05
            obj.ViewObject.Visibility = True
            doc.recompute()
            App.setActiveDocument(doc.Name)
            Gui.updateGui()
            view = Gui.getDocument(doc.Name).activeView()
            view.fitAll()
            view.setCameraType('Orthographic')
            eye = unit(vec(spec['eye']))
            right = unit((-eye).cross(vec(spec['up'])))
            up = unit(right.cross(-eye))
            rotation = App.Rotation(right, up, eye, 'ZXY')
            bbox = shape.BoundBox
            corners = [App.Vector(x, y, z)
                       for x in (bbox.XMin, bbox.XMax)
                       for y in (bbox.YMin, bbox.YMax)
                       for z in (bbox.ZMin, bbox.ZMax)]
            center = bbox.Center
            proj_x = [(p-center).dot(right) for p in corners]
            proj_y = [(p-center).dot(up) for p in corners]
            center += right*((max(proj_x)+min(proj_x))/2)
            center += up*((max(proj_y)+min(proj_y))/2)
            w, h = 1800, 1120
            height = max((max(proj_y)-min(proj_y))/.66,
                         (max(proj_x)-min(proj_x))/.58*h/w)
            camera = view.getCameraNode()
            camera.orientation.setValue(*rotation.Q)
            camera.height.setValue(height)
            distance = max(500, height*6)
            position = center + eye*distance
            camera.position.setValue(*tuple(position))
            camera.focalDistance.setValue(distance)
            camera.nearDistance.setValue(.1)
            camera.farDistance.setValue(max(500, height*6)*3)
            Gui.updateGui()
            raw = production / ('.' + spec['stem'] + '_native.png')
            view.saveImage(str(raw), w, h, 'White')
            native_image = QtGui.QImage(str(raw))
            shaded_samples = sum(1 for x in range(w//4, 3*w//4, 12)
                                 for y in range(h//5, 4*h//5, 12)
                                 if max(native_image.pixelColor(x, y).getRgb()[:3]) < 240)
            assert shaded_samples > 20, 'Native CAD image is empty: ' + spec['stem']
            sheet = QtGui.QImage(w, h+180, QtGui.QImage.Format_ARGB32)
            sheet.fill(QtGui.QColor('white'))
            painter = QtGui.QPainter(sheet)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
            painter.drawImage(0, 120, native_image)
            painter.setPen(QtGui.QColor('#1a2a3b'))
            painter.setFont(font(37, True))
            painter.drawText(QtCore.QRectF(40, 16, w-80, 55), QtCore.Qt.AlignLeft, spec['title'])
            painter.setFont(font(24))
            painter.setPen(QtGui.QColor('#4e5a65'))
            painter.drawText(QtCore.QRectF(42, 74, w-80, 37), QtCore.Qt.AlignLeft,
                             'QTY %d  |  RED: TAP  |  BLUE: NO TAP  |  Screw size at every screw hole' % spec['quantity'])

            def screen(p):
                q = p-center
                return QtCore.QPointF(w/2+q.dot(right)/height*h,
                                     120+h/2-q.dot(up)/height*h)

            annotations = []
            for hole in spec['holes']:
                inward = unit(vec(hole['inward_axis']))
                facing = (-inward).dot(eye)
                point = hole['point_mm']
                if facing < .12 and hole.get('exit_mm') and inward.dot(eye) > .12:
                    point = hole['exit_mm']
                    facing = inward.dot(eye)
                assert facing > .12, 'Hole hidden by selected view: ' + hole['id']
                anchor = screen(vec(point))
                annotations.append((anchor.x() < w/2, anchor, hole))
            labels = []
            for left in (True, False):
                group = sorted((a for a in annotations if a[0] == left), key=lambda a:a[1].y())
                for i, (_, anchor, hole) in enumerate(group):
                    y = 120+(h-88)/2 if len(group) == 1 else 260+i*710/(len(group)-1)
                    rect = QtCore.QRectF(35 if left else w-275, y, 240, 88)
                    color = QtGui.QColor('#c52335' if hole['tap'] else '#12579a')
                    pen = QtGui.QPen(color, 3)
                    painter.setPen(pen)
                    painter.setBrush(QtGui.QColor('white'))
                    painter.drawRect(rect)
                    painter.setFont(font(31, True))
                    label = ('\u00d86.2 ROD' if hole['spec']=='ROD' else hole['spec'])
                    label += '\nTAP' if hole['tap'] else '\nNO TAP'
                    painter.drawText(rect, QtCore.Qt.AlignCenter, label)
                    start = QtCore.QPointF(rect.right() if left else rect.left(), rect.center().y())
                    painter.drawLine(start, anchor)
                    dx, dy = anchor.x()-start.x(), anchor.y()-start.y()
                    length = math.hypot(dx, dy)
                    ux, uy = dx/length, dy/length
                    arrow = QtGui.QPolygonF([anchor,
                        QtCore.QPointF(anchor.x()-16*ux+6*uy,anchor.y()-16*uy-6*ux),
                        QtCore.QPointF(anchor.x()-16*ux-6*uy,anchor.y()-16*uy+6*ux)])
                    painter.setBrush(color)
                    painter.drawPolygon(arrow)
                    labels.append({'id': hole['id'], 'label': label,
                                   'anchor_pixel': [anchor.x(), anchor.y()]})
            painter.setPen(QtGui.QColor('#4e5a65'))
            painter.setFont(font(22))
            painter.drawText(QtCore.QRectF(42,h+135,w-84,34), QtCore.Qt.AlignLeft,
                             'Geometry: accompanying unchanged STEP  |  Native CAD rendering  |  2026-10-20')
            painter.end()
            target = production/(spec['stem']+'.png')
            assert sheet.save(str(target), 'PNG')
            raw.unlink()
            assert sha(source) == spec['step_sha256']
            records.append({'part':spec['stem'],'step_sha256':sha(source),'png_sha256':sha(target),
                            'holes': checks,'labels':labels,'native_step_valid':True,'solid_count':1})
        finally:
            App.closeDocument(doc.Name)
    if old_document:
        App.setActiveDocument(old_document.Name)
    result = {'date':'2026-10-20','geometry_changed':False,'parts':records,
              'hole_count':sum(len(p['holes']) for p in records),
              'thread_evidence':'production/drawing_holes.json',
              'rendering':'Native FreeCAD STEP rendering plus Qt vector text and leader annotations'}
    (production/'drawing_validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__ == '__main__':
    render(Path(__file__).resolve().parents[1])
