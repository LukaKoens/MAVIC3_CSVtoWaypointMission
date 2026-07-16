#DJIMavic3 #Drone 
[[DJI Mavic 3]]

# Process & Limits

The Mavic 3 only supports internal way point missions. so third party apps cannot be used to run orthomosaic missions, however, the way-point missions can be modified and pre generated for specific flights, this obviously just requires more planning and forethought than with something that can be setup on the fly. 

Not being able to create a waypoint mission without launching the drone is a pain.

## DJI fly waypoint file location

mtp://SAMSUNG_SAMSUNG_Android_RFCY21WKX7X/Internal%20storage/Android/data/dji.go.v5/files/waypoint

# Software options

- ## Waypoint Map
https://www.waypointmap.com/

this software seems good, however, for it to be viable for my use case, I will need the premium version, at 15 a month it's not super cheap, but definitely something worth trailing.

*Same guy https://www.droneinvoice.com/*

there is some issues with the default waypoint mission generation, becuase of the large swings DJI put in between points, to make the flights smooth, it doesn't have good overlap


- ## QGIS
	- ### Leveraging GeoFlight Planner & FlyPath
Need to explore if this works out of the box with a Mavic 3 it might not be GeoFlight Planner does.

just need to explore how well it exports to the waypoint mission, as it might provide a better alternative to Waypoint map without needing to develop anything custom.
 This won't work directly, GeoFlight Planner is made for Litchi missions, so would need to a) pay for Litchi, or b) figure out how to encode the missions into a Mavic 3 Compatible Waypoint mission,

GeoFlight Planner and a AI generated python conversion script seem to work. will need to trial this in practice, but this is super promising for developing a more robust workflow around the limitations present with the DJI mavic 3 and other none enterprise level drones

this process seems like it will be able to be developed into a more robust and user friendly workflow, there are a few steps required and DJI definitely doesn't make it easy but it is possible, and potentially practical

10/07/26 - Continuing to explore this method, various flights documented under [[FlightPlanning]] utilise this process.

## Usage of Lens and filters

- Polarised filters make nice images but they necessitate slower flight speeds or specific adjustments to the camera settings to account for the reduced light levels.