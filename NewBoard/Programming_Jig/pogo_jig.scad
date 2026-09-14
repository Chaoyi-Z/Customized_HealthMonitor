// Pogo-pin programming jig for the Animal Pulse Sensing flex board (CH591D).
// Coordinates = EasyEDA pick-and-place frame (mm). Pad centres measured from the board render (+/-0.05 mm):
//   5V [-6.086, -1.21], GND [-4.496, 1.105], D- [-2.779, -1.192], D+ [-1.139, 1.108]   pad dia 2.15 mm
// If you confirm different numbers in EasyEDA, just edit `pads` below and re-render.
$fn=48;
pads=[[-6.086,-1.210],[-4.496,1.105],[-2.779,-1.192],[-1.139,1.108]];                 // order: 5V, GND, D-, D+
pin_hole_d=1.2;   // 1.2 = press-fit P75 pogo pin (1.02 mm barrel) + CA glue; 1.4 for R75 receptacles
flex_t=0.25; pocket_depth=0.5; pocket_clear=0.2;
base_t=5.0; base_x=[-26.0,30.0]; base_y=[-16.0,16.0];
sensor_slot=[-18.6,-12.6,-4.5,4.5];   // through-slot under the MAX86916 (mounted on the bottom side of the head)
top_x=[-7.20,0.10]; top_y=[-15.0,15.0]; top_h=9.0;
magnetic=true;   // true: magnets + pegs (no screws). false: M3 screw version
mag_hole_d=4.15; mag_depth=2.1; mag_y=8.0;      // N52 disc magnets 4 x 2 mm (Amazon B0D6BCJCP7), 2 in base + 2 in carrier. Use mag_depth=3.1 for 4 x 3 mm discs
peg_d=3.0; peg_h=2.0; peg_y=12.3; peg_hole_d=3.3; peg_hole_depth=2.5;
tongue_w=4.7; bolt_x=-4.2; bolt_y=11.0; bolt_clear_d=3.3; bolt_tap_d=2.7;
outline=[[12.121,7.613],[12.556,7.505],[12.755,7.342],[12.882,7.142],[21.248,7.142],[21.719,6.997],[22.135,6.581],[22.244,6.183],[22.244,4.589],[22.280,4.553],[22.280,3.213],[22.317,3.176],[22.317,1.728],[22.353,1.691],[22.353,-0.156],[22.389,-0.192],[22.389,-2.039],[22.425,-2.075],[22.425,-2.981],[22.461,-3.017],[22.461,-5.299],[22.498,-5.335],[22.498,-6.748],[22.534,-6.784],[22.389,-7.291],[22.009,-7.671],[21.610,-7.780],[15.417,-7.780],[15.381,-7.816],[1.436,-7.816],[1.219,-7.780],[0.966,-7.671],[0.766,-7.508],[0.513,-7.037],[0.477,-6.820],[0.477,-3.307],[0.440,-3.162],[0.169,-2.818],[-0.266,-2.601],[-0.302,-2.637],[-0.338,-2.601],[-7.329,-2.601],[-7.383,-2.800],[-7.383,-3.307],[-7.491,-3.705],[-7.799,-4.013],[-8.198,-4.122],[-9.139,-4.122],[-9.176,-4.085],[-18.303,-4.085],[-19.100,-3.904],[-19.353,-3.723],[-19.516,-3.488],[-19.589,-3.162],[-19.589,-2.293],[-19.552,-2.256],[-19.480,3.032],[-19.335,3.502],[-19.136,3.738],[-18.919,3.883],[-18.484,3.991],[-8.125,3.919],[-7.727,3.810],[-7.419,3.502],[-7.310,3.104],[-7.329,2.434],[-0.338,2.434],[0.096,2.615],[0.296,2.814],[0.477,3.176],[0.477,6.183],[0.622,6.653],[0.821,6.889],[1.038,7.034],[1.473,7.142],[11.397,7.142],[11.523,7.342],[11.723,7.505],[12.121,7.613]];

module rbox(x0,x1,y0,y1,z0,z1) translate([x0,y0,z0]) cube([x1-x0,y1-y0,z1-z0]);
module base() difference() {
  rbox(base_x[0],base_x[1],base_y[0],base_y[1],0,base_t);
  translate([0,0,base_t-pocket_depth]) linear_extrude(pocket_depth+1) offset(r=pocket_clear) polygon(outline);
  rbox(sensor_slot[0],sensor_slot[1],sensor_slot[2],sensor_slot[3],-1,base_t+1);
  if(!magnetic) for(s=[-1,1]) translate([bolt_x,s*bolt_y,-1]) cylinder(d=bolt_tap_d,h=base_t+2);
  if(magnetic) for(s=[-1,1]) { translate([(top_x[0]+top_x[1])/2,s*mag_y,base_t-mag_depth]) cylinder(d=mag_hole_d,h=mag_depth+1);
                                translate([(top_x[0]+top_x[1])/2,s*peg_y,base_t-peg_hole_depth]) cylinder(d=peg_hole_d,h=peg_hole_depth+1); }
  rbox(base_x[0]-1,base_x[0]+3,base_y[1]-3,base_y[1]+1,-1,base_t+1);   // orientation key (corner notch)
}
module top() { z_floor=base_t-pocket_depth; z_tongue=z_floor+flex_t-0.05;
  difference() {
    union() { rbox(top_x[0],top_x[1],top_y[0],top_y[1],base_t,base_t+top_h);
              rbox(top_x[0],top_x[1],-tongue_w/2,tongue_w/2,z_tongue,base_t+0.01);
              if(magnetic) for(s=[-1,1]) translate([(top_x[0]+top_x[1])/2,s*peg_y,base_t-peg_h]) cylinder(d=peg_d,h=peg_h+0.01); }
    for(p=pads) translate([p[0],p[1],base_t-2]) cylinder(d=pin_hole_d,h=top_h+3);
    if(!magnetic) for(s=[-1,1]) translate([bolt_x,s*bolt_y,base_t-2]) cylinder(d=bolt_clear_d,h=top_h+3);
    if(magnetic) for(s=[-1,1]) translate([(top_x[0]+top_x[1])/2,s*mag_y,base_t-1]) cylinder(d=mag_hole_d,h=mag_depth+1);
    rbox(top_x[0]-1,top_x[0]+1.5,top_y[1]-1.5,top_y[1]+1,base_t-2,base_t+top_h+1);      // orientation key
  } }
// Assembly view (top sits on base). For printing export each part separately; print the top block tongue-side up (flip 180 deg).
base();
top();
