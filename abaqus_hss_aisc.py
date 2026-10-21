"""Conditional AISC 360-22 references for the generated rounded square HSS.

These are nominal equation references, not product/material qualification or an
LRFD/ASD design check. Actual model wall thickness is used, with no silently
inferred ASTM manufacturing route or 0.93 multiplier. Pn uses pinned K=1 and
the recorded member length; P0_effective excludes member buckling. Square HSS
has no F7 lateral-torsional buckling reduction. Nonprincipal bending and the
F7 slender-web AND slender-flange case deliberately have no assigned Mn.

Sources: ANSI/AISC 360-22 B4, E3, E7, F7, H1; rounded corner moments are
integrated analytically through the finite wall, rather than sharp-corner or
shell centreline approximations. H1 is plotted with nominal strengths only;
it is explicitly not an available-strength interaction/design check.
"""
import csv
import json
import math
from pathlib import Path


def rounded_properties(B, H, t, radius):
    B, H, t, radius = map(float, (B, H, t, radius))
    if not all(math.isfinite(v) and v > 0 for v in (B,H,t,radius)):
        raise ValueError('Rounded HSS dimensions must be finite and positive')
    ro, ri = radius+t/2., radius-t/2.
    if ri < 0 or min(B,H) <= 2*ro or min(B,H) <= 2*t:
        raise ValueError('Invalid finite-wall HSS corner geometry')
    def solid(b,h,r):
        a = (1-math.pi/4)*r*r
        first = r**3/6.
        second = r**4*(1/3.-math.pi/16.)
        cx, cy = b/2-r, h/2-r
        return (b*h-4*a,
                b*h**3/12-4*(cy*cy*a+2*cy*first+second),
                h*b**3/12-4*(cx*cx*a+2*cx*first+second),
                b*h*h/4-4*(cy*a+first),
                h*b*b/4-4*(cx*a+first))
    values = [a-b for a,b in zip(solid(B,H,ro),solid(B-2*t,H-2*t,ri))]
    area, ix, iz, zx, zz = values
    return dict(area_mm2=area,Ix_mm4=ix,Iz_mm4=iz,Zx_mm3=zx,Zz_mm3=zz,
                Sx_mm3=ix/(H/2),Sz_mm3=iz/(B/2),outside_radius_mm=ro,
                inside_radius_mm=ri,flat_b_mm=B-2*ro,flat_h_mm=H-2*ro)


def effective_width(b,t,E,Fy,Fn):
    """AISC E7-2 through E7-5, square/rectangular HSS c1=0.20."""
    lam, lr = b/t, 1.40*math.sqrt(E/Fy)
    if lam <= lr*math.sqrt(Fy/Fn):
        return b
    c1 = .20
    c2 = (1-math.sqrt(1-4*c1))/(2*c1)
    fel = (c2*lr/lam)**2*Fy
    ratio = math.sqrt(fel/Fn)
    return max(0.,min(b,b*(1-c1*ratio)*ratio))


def nominal_reference(cfg, angle=0., pinned=True):
    """Fail closed outside the implemented square-HSS principal-axis scope."""
    B,H,t,R = [float(cfg[k]) for k in ('B_mm','H_mm','t_mm','radius_mm')]
    E,Fy,L = [float(cfg[k]) for k in ('E_MPa','fy_MPa','length_mm')]
    if not all(math.isfinite(x) and x>0 for x in (E,Fy,L)):
        raise ValueError('Invalid HSS material or member length')
    g=rounded_properties(B,H,t,R)
    area=g['area_mm2']; b=g['flat_b_mm']; h=g['flat_h_mm']
    rmin=math.sqrt(min(g['Ix_mm4'],g['Iz_mm4'])/area)
    fe=math.pi**2*E/(L/rmin)**2
    fn=(.658**(Fy/fe))*Fy if Fy/fe<=2.25 else .877*fe
    def ae(stress):
        return area-2*t*((b-effective_width(b,t,E,Fy,stress))+
                         (h-effective_width(h,t,E,Fy,stress)))
    result=dict(standard='ANSI/AISC 360-22',geometry=g,angle_deg=float(angle),
        B_mm=B,H_mm=H,t_mm=t,centreline_radius_mm=R,length_mm=L,E_MPa=E,fy_MPa=Fy,
        thickness_basis='actual Abaqus model wall; no product thickness reduction inferred',
        scope='conditional equation reference; material/product compliance not established',
        Py_kN=Fy*area/1000.,P0_effective_kN=Fy*ae(Fy)/1000.,
        Pn_kN=fn*ae(fn)/1000. if pinned else None,Fn_MPa=fn,
        Ae_member_mm2=ae(fn),K=1. if pinned else None,
        axial_basis='E3/E7 flexural buckling; pinned model K=1' if pinned else 'effective length unknown; member Pn not assigned',
        Mn_kNm=None,flexure_status='UNSUPPORTED_RECTANGULAR_OR_NONPRINCIPAL_AXIS')
    # The generator has square HSS. Do not quietly generalize its bracing,
    # torsion or nonprincipal-axis assumptions to other section/member models.
    if not math.isclose(B,H,rel_tol=1e-8) or not math.isclose(float(angle)%90.,0.,abs_tol=1e-7):
        return result
    # For this square closed section, the St Venant term alone bounds the
    # torsional elastic critical stress; no positive warping term is needed.
    midarea=(B-t)*(H-t)-(4-math.pi)*R*R
    perimeter=2*(B+H-2*t)-8*R+2*math.pi*R
    j=4*midarea**2*t/perimeter
    nu=float(cfg.get('nu',.3))
    ft=E/(2*(1+nu))*j/(g['Ix_mm4']+g['Iz_mm4'])
    result['torsional_elastic_lower_bound_MPa']=ft
    if ft < fe:
        # Avoid a flexural-only Pn claim if torsion might govern.
        result['Pn_kN']=None
        result['axial_basis']='torsional buckling requires separate E4 assessment'
    root=math.sqrt(E/Fy)
    lf,lw=b/t,h/t
    fp,fr=1.12*root,1.40*root
    wp,wr=2.42*root,5.70*root
    result.update(flange_slenderness=lf,web_slenderness=lw,
        flange_class='compact' if lf<=fp else 'noncompact' if lf<=fr else 'slender',
        web_class='compact' if lw<=wp else 'noncompact' if lw<=wr else 'slender',
        web_noncompact_limit=wr,My_kNm=Fy*g['Sx_mm3']/1e6,Mp_kNm=Fy*g['Zx_mm3']/1e6)
    if lw>wr:
        result['flexure_status']='OUTSIDE_F7_SLENDER_WEB_AND_FLANGE'
        return result
    mp, my = Fy*g['Zx_mm3'],Fy*g['Sx_mm3']
    mnflange=mp
    if lf>fr:
        be=min(b,1.92*t*root*(1-.38*root/lf))
        removed=(b-be)*t; cy=H/2-t/2
        effective_area=area-removed
        centroid=-removed*cy/effective_area
        ie=g['Ix_mm4']-removed*(t*t/12+cy*cy)-effective_area*centroid*centroid
        mnflange=Fy*ie/(H/2-centroid)
        result.update(effective_flange_width_mm=be,effective_centroid_mm=centroid)
    elif lf>fp:
        mnflange=mp-(mp-my)*(lf-fp)/(fr-fp)
    mnweb=mp if lw<=wp else mp-(mp-my)*(lw-wp)/(wr-wp)
    result.update(Mn_kNm=min(mp,mnflange,mnweb)/1e6,
        flexure_status='F7_SECTION_REFERENCE',flange_Mn_kNm=mnflange/1e6,web_Mn_kNm=mnweb/1e6)
    return result


def convexity_diagnostic(coordinates):
    """Test the origin-closed sampled domain, never compute an outer hull."""
    xy=[(float(m),float(p)) for m,p in coordinates]
    if not xy or any(not all(math.isfinite(v) and v>=0 for v in pair) for pair in xy):
        return dict(status='INSUFFICIENT_OR_SIGNED_SAMPLES')
    sx=max(x for x,y in xy);sy=max(y for x,y in xy)
    if not sx or not sy or not any(abs(x)<1e-8 and y>0 for x,y in xy) or not any(abs(y)<1e-8 and x>0 for x,y in xy):
        return dict(status='INCOMPLETE_ENDPOINTS')
    if len(xy)<3:
        return dict(status='INSUFFICIENT_OR_SIGNED_SAMPLES')
    xy=sorted(set((x/sx,y/sy) for x,y in xy),key=lambda v:math.atan2(v[0],v[1]))
    polygon=[(0.,0.)]+xy
    area=sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(polygon,polygon[1:]+polygon[:1]))
    crosses=[]
    for i,a in enumerate(polygon):
        b=polygon[(i+1)%len(polygon)];c=polygon[(i+2)%len(polygon)]
        crosses.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
    bad=[i for i,v in enumerate(crosses) if v*area < -1e-10]
    return dict(status='NONCONVEX_SAMPLES' if bad else 'CONVEX_SAMPLES',
                corners_requiring_review=bad,
                interpretation='geometric screening only; neither convexity nor a hull verifies stability or capacity')


def references_for_points(points,angle):
    """Read existing HSS geometry; never derive its radius from a thickness rule."""
    references=[];seen=set()
    for point in points:
        folder=point.get('folder') or point.get('dataset')
        if not folder or str(folder) in seen:continue
        seen.add(str(folder));path=Path(folder)/'hss_manifest.json'
        if not path.is_file():continue
        manifest=json.loads(path.read_text(encoding='utf-8-sig'))
        cfg=manifest['geometry']
        signature=json.dumps(cfg,sort_keys=True)
        if any(item.get('geometry_signature')==signature for item in references):continue
        pinned=manifest.get('end_condition')=='both ends U1 U2 UR3=0; one middle U3 anchor; free end warping'
        item=nominal_reference(cfg,angle,pinned=pinned)
        item.update(source_manifest=str(path),geometry_signature=signature)
        references.append(item)
    return references


def reference_notes(references):
    notes=[]
    for r in references:
        pn,mn=r.get('Pn_kN'),r.get('Mn_kNm')
        if mn is None:
            note=('HSS: AISC Mn unavailable: slender web + flange (F7).'
                  if r['flexure_status']=='OUTSIDE_F7_SLENDER_WEB_AND_FLANGE'
                  else 'HSS: AISC Mn not assigned for this axis/section.')
        else:
            note=('HSS nominal: Pn %.1f kN; Mn %.2f kN m.'%(pn,mn) if pn is not None
                  else 'HSS nominal Mn %.2f kN m; Pn requires assessment.'%mn)
        notes.append(note)
    if notes:
        notes.append('Actual model thickness; rounded corners; no phi/Omega.')
    return notes


def draw_references(ax,references,scales=(1.,1.),notes=True):
    """Distinct HSS nominal markers; never relabel Fy*A or Fy*Z as Pn/Mn."""
    ms,ps=scales;lines=[]
    for i,r in enumerate(references):
        name='HSS' if len(references)==1 else 'HSS %d'%(i+1)
        pn,mn=r.get('Pn_kN'),r.get('Mn_kNm')
        if pn is not None:
            line=ax.axhline(pn/ps,color='#8a3f97',ls=(0,(5,3)),lw=1.2,label='%s AISC Pn = %.1f kN (K=1)'%(name,pn));line.set_gid('hss-aisc-pn');lines.append(line)
        if mn is not None:
            line=ax.axvline(mn/ms,color='#8a3f97',ls=(0,(2,2)),lw=1.2,label='%s AISC Mn = %.2f kN m'%(name,mn));line.set_gid('hss-aisc-mn');lines.append(line)
        if pn is not None and mn is not None:
            line,=ax.plot([0.,.9*mn/ms,mn/ms],[pn/ps,.2*pn/ps,0.],color='#8a3f97',ls='-.',lw=1.4,label='HSS H1 nominal-strength reference (no phi/Omega)');line.set_gid('hss-aisc-h1');lines.append(line)
    if notes and references:
        ax.text(.98,.98,'\n'.join(reference_notes(references)),transform=ax.transAxes,
                fontsize=7,color='#743981',ha='right',va='top',
                bbox=dict(fc='white',ec='none',alpha=.85))
    if lines:
        xlim,ylim=ax.get_xlim(),ax.get_ylim()
        ax.set_xlim(xlim[0],max(xlim[1],max((r.get('Mn_kNm') or 0)/ms*1.05 for r in references)))
        ax.set_ylim(ylim[0],max(ylim[1],max((r.get('Pn_kN') or 0)/ps*1.05 for r in references)))
    return lines


def write_reference_report(directory,points,angles):
    records=[r for angle in angles for r in references_for_points(points,angle)]
    if not records:return []
    path=Path(directory)/'HSS_AISC360_nominal_reference.json'
    path.write_text(json.dumps(dict(schema='hss_aisc360_22_model_reference_v1',references=records),indent=2,allow_nan=False),encoding='utf-8')
    table=path.with_suffix('.csv')
    fields=('angle_deg','B_mm','H_mm','t_mm','centreline_radius_mm','length_mm','fy_MPa','Py_kN','P0_effective_kN','Pn_kN','My_kNm','Mp_kNm','Mn_kNm','flange_class','web_class','flexure_status','axial_basis','thickness_basis','scope','source_manifest')
    with table.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(records)
    return [str(path),str(table)]
